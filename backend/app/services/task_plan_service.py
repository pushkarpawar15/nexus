from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.core.config import Settings
from app.core.errors import ErrorCode, NexusError
from app.integrations.attachments import AttachmentStorage
from app.models.task import (
    ExecutionPlan,
    PlanSource,
    Task,
    TaskAttachment,
    TaskStatus,
    TransitionRecord,
)
from app.policies.workforce import WorkforcePolicy
from app.repositories.records import WorkflowRepositories
from app.repositories.tasks import TaskRepository
from app.schemas.execution import ActivityEvent, ActivitySource
from app.schemas.tasks import UpdateExecutionPlanRequest
from app.services.workflow_service import WorkflowService


class TaskPlanService:
    def __init__(
        self,
        settings: Settings,
        tasks: TaskRepository,
        records: WorkflowRepositories,
        attachment_storage: AttachmentStorage | None = None,
    ) -> None:
        self.settings, self.tasks, self.records = settings, tasks, records
        self.attachment_storage = attachment_storage
        self.policy = WorkforcePolicy()

    def _plan(self, task: Task, request: UpdateExecutionPlanRequest) -> ExecutionPlan:
        decision = self.policy.evaluate(
            risk=request.risk,
            task_type=task.task_type,
            complexity=request.complexity,
            available_capabilities={"plan", "ai_execute", "ai_validate", "security_review"},
            permissions=self.settings.ai_permissions,
            recommended_mode=request.mode,
            required_approval=request.approval_required,
            operator_override=True,
        )
        if decision.mode != request.mode:
            raise NexusError(
                ErrorCode.INVALID_TRANSITION,
                f"Configured capabilities do not support {request.mode.value} mode: "
                f"{decision.reason}",
                status_code=409,
            )
        unknown_agents = set(request.assigned_agents) - set(decision.allowed_workers)
        if unknown_agents:
            raise NexusError(
                ErrorCode.VALIDATION_ERROR,
                "Selected agents are unavailable: " + ", ".join(sorted(unknown_agents)),
                status_code=422,
            )
        agents = list(dict.fromkeys(request.assigned_agents))
        if request.mode.value in {"AI", "HYBRID"}:
            for required in ("coder", "validator"):
                if required not in agents:
                    agents.append(required)
        if decision.security_review_required and "security_reviewer" not in agents:
            agents.append("security_reviewer")
        approval_required = request.approval_required or decision.approval_required
        return ExecutionPlan(
            risk=request.risk,
            complexity=request.complexity,
            mode=request.mode,
            assigned_agents=agents,
            approval_required=approval_required,
            reason=request.reason.strip(),
            steps=[item.strip() for item in request.steps],
            expected_files_or_areas=[item.strip() for item in request.expected_files_or_areas],
            validation_plan=[item.strip() for item in request.validation_plan],
            potential_concerns=[item.strip() for item in request.potential_concerns],
            source=PlanSource.OPERATOR,
            model_id="operator-edited",
            policy_reason=decision.reason,
            security_review_required=decision.security_review_required,
            operator_override=True,
        )

    async def update(self, task: Task, request: UpdateExecutionPlanRequest, actor: str) -> Task:
        if task.execution_plan is None:
            raise NexusError(
                ErrorCode.TASK_CONFLICT,
                "Analyze the task before editing its plan.",
                status_code=409,
            )
        if task.status not in {
            TaskStatus.BACKLOG,
            TaskStatus.READY,
            TaskStatus.HUMAN_QUEUE,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
        }:
            raise NexusError(
                ErrorCode.TASK_CONFLICT,
                "A plan cannot be edited while task execution or review is in progress.",
                status_code=409,
            )
        plan = self._plan(task, request)
        now = datetime.now(timezone.utc)
        if now <= task.updated_at:
            now = task.updated_at + timedelta(microseconds=1)
        destination = task.status
        if task.status in {TaskStatus.READY, TaskStatus.HUMAN_QUEUE}:
            destination = TaskStatus.HUMAN_QUEUE if plan.mode.value == "HUMAN" else TaskStatus.READY
        history = list(task.transition_history)
        if destination != task.status:
            history.append(
                TransitionRecord(
                    from_status=task.status,
                    to_status=destination,
                    actor=actor,
                    reason="Operator updated the execution plan and mode.",
                    at=now,
                )
            )
        updated = task.model_copy(
            update={
                "title": request.title.strip() if request.title else task.title,
                "risk": plan.risk,
                "complexity": plan.complexity,
                "mode": plan.mode,
                "assigned_agents": plan.assigned_agents,
                "approval_required": plan.approval_required,
                "execution_plan": plan,
                "status": destination,
                "transition_history": history,
                "updated_at": now,
            }
        )
        try:
            saved = await self.tasks.replace(updated, expected_updated_at=task.updated_at)
        except (RuntimeError, KeyError) as exc:
            raise NexusError(
                ErrorCode.TASK_CONFLICT,
                "Task changed while the plan was being saved.",
                status_code=409,
            ) from exc
        await self._event(
            saved,
            "analysis.plan_updated",
            "Operator edited the analysis plan.",
            {"source": PlanSource.OPERATOR.value, "mode": plan.mode.value, "risk": plan.risk.value},
        )
        return saved

    async def clone(self, task: Task, request: UpdateExecutionPlanRequest, actor: str) -> Task:
        if task.execution_plan is None:
            raise NexusError(
                ErrorCode.TASK_CONFLICT,
                "Analyze the task before cloning its plan.",
                status_code=409,
            )
        plan = self._plan(task, request)
        now = datetime.now(timezone.utc)
        title = (request.title or f"{task.title} (plan copy)").strip()[:200]
        target = self.settings.approved_repositories.get(task.repository_key)
        if target is None:
            raise NexusError(
                ErrorCode.REPOSITORY_NOT_ALLOWED, "Repository is not approved.", status_code=422
            )
        clone_id = uuid4().hex
        attachments: list[TaskAttachment] = []
        if task.attachments and self.attachment_storage is None:
            raise NexusError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "Task attachment storage is unavailable, so attachments could not be cloned.",
                status_code=503,
            )
        try:
            for attachment in task.attachments:
                attachment_id = uuid4().hex
                content = await self.attachment_storage.get(attachment.storage_key)
                storage_key = await self.attachment_storage.put(
                    clone_id, attachment_id, content, attachment.content_type
                )
                attachments.append(
                    attachment.model_copy(
                        update={
                            "id": attachment_id,
                            "storage_key": storage_key,
                            "created_at": now,
                            "created_by": actor,
                        }
                    )
                )
        except Exception:
            for attachment in attachments:
                await self.attachment_storage.delete(attachment.storage_key)
            raise
        clone = Task(
            id=clone_id,
            title=title,
            description=task.description,
            repository_key=task.repository_key,
            repository_label=task.repository_label or target.label,
            task_type=task.task_type,
            status=TaskStatus.BACKLOG,
            created_at=now,
            updated_at=now,
            created_by=actor,
            attachments=attachments,
        )
        try:
            clone = await self.tasks.create(clone)
        except Exception:
            if self.attachment_storage is not None:
                for attachment in attachments:
                    await self.attachment_storage.delete(attachment.storage_key)
            raise
        workflow = WorkflowService(self.tasks)
        clone = await workflow.transition(
            clone,
            TaskStatus.ANALYZING,
            actor=actor,
            reason=f"Cloned plan from task {task.id}.",
            trusted=True,
        )
        clone = clone.model_copy(
            update={
                "risk": plan.risk,
                "complexity": plan.complexity,
                "mode": plan.mode,
                "assigned_agents": plan.assigned_agents,
                "approval_required": plan.approval_required,
                "execution_plan": plan,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        clone = await self.tasks.replace(clone, expected_updated_at=clone.updated_at)
        clone = await workflow.transition(
            clone,
            TaskStatus.TRIAGED,
            actor=actor,
            reason="Cloned operator-edited plan.",
            trusted=True,
        )
        destination = TaskStatus.HUMAN_QUEUE if plan.mode.value == "HUMAN" else TaskStatus.READY
        clone = await workflow.transition(
            clone,
            destination,
            actor=actor,
            reason="Cloned plan is ready for a separate task execution.",
            trusted=True,
        )
        await self._event(
            clone,
            "analysis.plan_cloned",
            f"Plan cloned from task {task.id}; the original task was left unchanged.",
            {"source_task_id": task.id},
        )
        return clone

    async def _event(self, task: Task, event_type: str, message: str, metadata: dict) -> None:
        await self.records.activity_events.create(
            ActivityEvent(
                id=uuid4().hex,
                task_id=task.id,
                worker_name="planner",
                event_type=event_type,
                message=message,
                occurred_at=datetime.now(timezone.utc),
                source=ActivitySource.SYSTEM,
                metadata=metadata,
            )
        )
