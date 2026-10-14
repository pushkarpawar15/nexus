from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.core.config import Settings
from app.core.errors import ErrorCode, NexusError
from app.core.logging import redact_credentials
from app.models.task import (
    ExecutionMode,
    Task,
    TaskStatus,
)
from app.policies.workforce import WorkforcePolicy
from app.repositories.records import WorkflowRepositories
from app.repositories.tasks import TaskRepository
from app.schemas.execution import ActivityEvent, ActivitySource, ExecutionRecord, ExecutionStatus
from app.services.model_catalog import validate_model_id
from app.services.workflow_service import WorkflowService


class ExecutionService:
    def __init__(
        self,
        settings: Settings,
        tasks: TaskRepository,
        records: WorkflowRepositories,
        dispatcher=None,
    ) -> None:
        self.settings, self.tasks, self.records = settings, tasks, records
        self.dispatcher = dispatcher

    async def start(
        self, task: Task, actor: str, model_id: str | None = None
    ) -> ExecutionRecord:
        model_id = validate_model_id(model_id, self.settings.gemini_coding_model)
        if (
            self.dispatcher is None
            or not self.settings.coding_enabled
            or not self.settings.validation_enabled
        ):
            backend = (
                "Cloud Run" if self.settings.execution_backend == "cloud_run_jobs" else "local"
            )
            raise NexusError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                f"{backend} coding and validation are disabled or unavailable. Configure the "
                f"{backend} execution worker and validator before starting execution.",
                status_code=503,
            )
        if task.status != TaskStatus.READY:
            raise NexusError(
                ErrorCode.EXECUTION_CONFLICT,
                "Only READY tasks can start AI execution.",
                status_code=409,
            )
        if task.mode not in {ExecutionMode.AI, ExecutionMode.HYBRID} or not task.execution_plan:
            raise NexusError(
                ErrorCode.EXECUTION_CONFLICT, "Task has no executable AI plan.", status_code=409
            )
        if "coder" not in task.assigned_agents or "validator" not in task.assigned_agents:
            raise NexusError(
                ErrorCode.EXECUTION_CONFLICT,
                "Both coder and validator must be assigned.",
                status_code=409,
            )
        if task.risk is None or task.complexity is None:
            raise NexusError(
                ErrorCode.EXECUTION_CONFLICT,
                "Risk and complexity must be assessed before execution.",
                status_code=409,
            )
        decision = WorkforcePolicy().evaluate(
            risk=task.risk,
            task_type=task.task_type,
            complexity=task.complexity,
            available_capabilities={"plan", "ai_execute", "ai_validate", "security_review"},
            permissions=self.settings.ai_permissions,
            recommended_mode=task.mode,
            required_approval=bool(task.approval_required),
            operator_override=task.execution_plan.operator_override,
        )
        if decision.mode not in {ExecutionMode.AI, ExecutionMode.HYBRID}:
            raise NexusError(
                ErrorCode.EXECUTION_CONFLICT,
                "Current policy does not permit AI execution for this task.",
                status_code=409,
            )
        if (
            task.execution_plan.security_review_required
            and "security_reviewer" not in task.assigned_agents
        ):
            raise NexusError(
                ErrorCode.EXECUTION_CONFLICT,
                "The required security reviewer is not assigned.",
                status_code=409,
            )
        now = datetime.now(timezone.utc)
        record = ExecutionRecord(
            id=uuid4().hex,
            task_id=task.id,
            assigned_worker="coder",
            status=ExecutionStatus.QUEUED,
            created_at=now,
            updated_at=now,
            repository_key=task.repository_key,
            model_id=model_id,
        )
        await self.records.executions.create(record)
        await self._event(task.id, record.id, "execution.queued", "Execution attempt was queued.")
        await WorkflowService(self.tasks).transition(
            task,
            TaskStatus.IN_PROGRESS,
            actor=actor,
            reason="AI execution queued",
            trusted=True,
        )
        accepted = await self.dispatcher.dispatch(record.id)
        await self._event(
            task.id,
            record.id,
            "execution.dispatched" if accepted else "execution.blocked",
            "Execution worker accepted the attempt."
            if accepted
            else "Execution worker did not accept the attempt.",
        )
        if not accepted:
            blocked = record.model_copy(
                update={
                    "status": ExecutionStatus.BLOCKED,
                    "updated_at": datetime.now(timezone.utc),
                    "finished_at": datetime.now(timezone.utc),
                    "error_details": "Execution worker did not accept the attempt.",
                    "final_result": "BLOCKED",
                }
            )
            await self.records.executions.replace(blocked, expected_updated_at=record.updated_at)
            current = await self.tasks.get(task.id)
            if current:
                await WorkflowService(self.tasks).transition(
                    current,
                    TaskStatus.BLOCKED,
                    actor="system",
                    reason=blocked.error_details,
                    trusted=True,
                )
            return blocked
        return record

    async def retry(
        self, task: Task, actor: str, model_id: str | None = None
    ) -> ExecutionRecord:
        interrupted_statuses = {
            TaskStatus.IN_PROGRESS,
            TaskStatus.VALIDATING,
            TaskStatus.SECURITY_REVIEW,
        }
        interrupted = task.status in interrupted_statuses
        if task.status not in {TaskStatus.FAILED, TaskStatus.BLOCKED} and not interrupted:
            raise NexusError(
                ErrorCode.EXECUTION_CONFLICT,
                "Only failed or blocked tasks can retry execution.",
                status_code=409,
            )
        if task.mode not in {ExecutionMode.AI, ExecutionMode.HYBRID} or task.execution_plan is None:
            raise NexusError(
                ErrorCode.EXECUTION_CONFLICT,
                "This task has no AI execution plan to retry. Re-analyze it or choose an "
                "AI or hybrid mode.",
                status_code=409,
            )
        if (
            self.dispatcher is None
            or not self.settings.coding_enabled
            or not self.settings.validation_enabled
        ):
            backend = (
                "Cloud Run" if self.settings.execution_backend == "cloud_run_jobs" else "local"
            )
            raise NexusError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                f"{backend} coding and validation are disabled or unavailable. Configure the "
                f"{backend} execution worker and validator before retrying execution.",
                status_code=503,
            )
        if interrupted:
            executions = await self.records.executions.list_for_task(task.id)
            if not executions or executions[0].status != ExecutionStatus.CANCELLED:
                raise NexusError(
                    ErrorCode.EXECUTION_CONFLICT,
                    "An active task can only be recovered when its latest execution was cancelled.",
                    status_code=409,
                )
            task = await WorkflowService(self.tasks).transition(
                task,
                TaskStatus.BLOCKED,
                actor=actor,
                reason=(
                    "The latest execution was cancelled before the task status was updated; "
                    "operator retry is starting a fresh attempt."
                ),
                trusted=True,
            )
        ready = await WorkflowService(self.tasks).transition(
            task,
            TaskStatus.READY,
            actor=actor,
            reason="Operator retried execution using the existing analysis plan.",
            trusted=True,
        )
        return await self.start(ready, actor, model_id)

    async def provide_input(self, task: Task, answer: str, actor: str) -> ExecutionRecord:
        if (
            task.status not in {TaskStatus.AWAITING_INPUT, TaskStatus.BLOCKED}
            or not task.pending_question
        ):
            raise NexusError(
                ErrorCode.EXECUTION_CONFLICT,
                "This task is not waiting for human input.",
                status_code=409,
            )
        safe_answer = redact_credentials(answer.strip())
        guidance = "\n\n".join(
            value for value in (task.human_guidance, safe_answer) if value
        )[-8000:]
        saved_guidance = task.model_copy(
            update={"human_guidance": guidance, "updated_at": datetime.now(timezone.utc)}
        )
        try:
            await self.tasks.replace(saved_guidance, expected_updated_at=task.updated_at)
        except (RuntimeError, KeyError) as exc:
            raise NexusError(
                ErrorCode.TASK_CONFLICT,
                "Task changed while operator guidance was being submitted.",
                status_code=409,
            ) from exc
        resume = getattr(self.dispatcher, "provide_input", None)
        if task.pending_execution_id and resume is not None:
            if await resume(task.pending_execution_id, safe_answer):
                execution = await self.records.executions.get(task.pending_execution_id)
                if execution is not None:
                    return execution
            discard = getattr(self.dispatcher, "discard_input_checkpoint", None)
            if discard is not None:
                await discard(task.pending_execution_id)
        task = await self.tasks.get(task.id) or saved_guidance
        if task.pending_execution_id:
            previous = await self.records.executions.get(task.pending_execution_id)
            if previous and previous.status in {ExecutionStatus.QUEUED, ExecutionStatus.RUNNING}:
                now = datetime.now(timezone.utc)
                await self.records.executions.replace(
                    previous.model_copy(
                        update={
                            "status": ExecutionStatus.BLOCKED,
                            "updated_at": now,
                            "finished_at": now,
                            "error_details": (
                                "The local worker was unavailable when guidance arrived; "
                                "a fresh execution will use the saved guidance."
                            ),
                            "final_result": "OPERATOR_INPUT_FRESH_EXECUTION",
                        }
                    ),
                    expected_updated_at=previous.updated_at,
                )
                await self._event(
                    task.id,
                    previous.id,
                    "execution.resume_as_new_attempt",
                    "The previous local worker was unavailable; saved guidance will continue "
                    "in a fresh workspace.",
                )
                await self._event(
                    task.id,
                    previous.id,
                    "human.input_received",
                    "Operator guidance was saved for the next coding attempt.",
                    {"guidance": guidance},
                )
        ready = await WorkflowService(self.tasks).transition(
            task,
            TaskStatus.READY,
            actor=actor,
            reason=(
                "Operator answered after the previous worker stopped; a fresh execution "
                "will continue."
            ),
            trusted=True,
            updates={
                "pending_question": None,
                "pending_execution_id": None,
            },
        )
        return await self.start(ready, actor)

    async def stop(self, task: Task, execution_id: str, actor: str) -> ExecutionRecord:
        execution = await self.records.executions.get(execution_id)
        if execution is None or execution.task_id != task.id:
            raise NexusError(
                ErrorCode.EXECUTION_NOT_FOUND,
                "Execution was not found for this task.",
                status_code=404,
            )
        if execution.status not in {ExecutionStatus.QUEUED, ExecutionStatus.RUNNING}:
            raise NexusError(
                ErrorCode.EXECUTION_CONFLICT,
                "This execution has already finished and cannot be stopped.",
                status_code=409,
            )
        cancel = getattr(self.dispatcher, "cancel", None)
        if cancel is None:
            raise NexusError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "The active execution worker cannot accept stop requests.",
                status_code=503,
            )
        reason = "Execution stop requested by an operator."
        await self._event(task.id, execution.id, "execution.stop_requested", reason)
        await cancel(execution.id)

        current_execution = await self.records.executions.get(execution.id)
        if current_execution is None:
            raise NexusError(
                ErrorCode.EXECUTION_NOT_FOUND, "Execution was not found.", status_code=404
            )
        if current_execution.status not in {
            ExecutionStatus.QUEUED,
            ExecutionStatus.RUNNING,
            ExecutionStatus.CANCELLED,
        }:
            raise NexusError(
                ErrorCode.EXECUTION_CONFLICT,
                "Execution finished before the stop request could take effect.",
                status_code=409,
            )
        if current_execution.status in {ExecutionStatus.QUEUED, ExecutionStatus.RUNNING}:
            now = datetime.now(timezone.utc)
            expected_updated_at = current_execution.updated_at
            current_execution = current_execution.model_copy(
                update={
                    "status": ExecutionStatus.CANCELLED,
                    "updated_at": now,
                    "finished_at": now,
                    "error_details": "Execution was stopped by an operator.",
                    "final_result": "CANCELLED",
                }
            )
            await self.records.executions.replace(
                current_execution,
                expected_updated_at=expected_updated_at,
            )
            await self._event(
                task.id,
                execution.id,
                "execution.cancelled",
                "Execution stopped by an operator.",
            )
        current_task = await self.tasks.get(task.id)
        if current_task and current_task.status in {
            TaskStatus.IN_PROGRESS,
            TaskStatus.VALIDATING,
            TaskStatus.SECURITY_REVIEW,
        }:
            await WorkflowService(self.tasks).transition(
                current_task,
                TaskStatus.BLOCKED,
                actor=actor,
                reason="Execution stopped by an operator; its workspace was removed.",
                trusted=True,
            )
        return current_execution

    async def _event(
        self,
        task_id: str,
        execution_id: str,
        event_type: str,
        message: str,
        metadata: dict | None = None,
    ) -> None:
        await self.records.activity_events.create(
            ActivityEvent(
                id=uuid4().hex,
                task_id=task_id,
                execution_id=execution_id,
                worker_name="system",
                event_type=event_type,
                message=message,
                occurred_at=datetime.now(timezone.utc),
                source=ActivitySource.SYSTEM,
                metadata=metadata or {},
            )
        )


__all__ = ["ExecutionService"]
