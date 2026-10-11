from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.core.config import Settings
from app.core.errors import ErrorCode, NexusError
from app.models.task import (
    ExecutionMode,
    RiskLevel,
    Task,
    TaskStatus,
    TransitionRecord,
)
from app.policies.workforce import WorkforcePolicy
from app.repositories.records import WorkflowRepositories
from app.repositories.tasks import TaskRepository
from app.schemas.execution import ActivityEvent, ActivitySource


class TaskModeService:
    """Allows an operator to choose a policy-approved mode before execution starts."""

    def __init__(
        self,
        settings: Settings,
        tasks: TaskRepository,
        records: WorkflowRepositories,
    ) -> None:
        self.settings = settings
        self.tasks = tasks
        self.records = records
        self.policy = WorkforcePolicy()

    async def set_mode(
        self,
        task: Task,
        mode: ExecutionMode,
        actor: str,
        *,
        risk: RiskLevel | None = None,
        risk_reason: str | None = None,
    ) -> Task:
        if task.status not in {TaskStatus.READY, TaskStatus.HUMAN_QUEUE}:
            raise NexusError(
                ErrorCode.INVALID_TRANSITION,
                "Execution mode can only be changed before work starts.",
                status_code=409,
            )
        if task.execution_plan is None or task.risk is None or task.complexity is None:
            raise NexusError(
                ErrorCode.INVALID_TRANSITION,
                "A completed analysis plan is required before choosing a mode.",
                status_code=409,
            )
        selected_risk = risk or task.risk
        if selected_risk is None:
            raise NexusError(
                ErrorCode.INVALID_TRANSITION,
                "A risk assessment is required before choosing a mode.",
                status_code=409,
            )
        risk_changed = selected_risk != task.risk
        mode_changed = task.mode != mode
        if not mode_changed and not risk_changed:
            return task
        if risk_changed and not (risk_reason and risk_reason.strip()):
            raise NexusError(
                ErrorCode.VALIDATION_ERROR,
                "Provide a reason when changing the risk assessment.",
                status_code=422,
            )
        decision = self.policy.evaluate(
            risk=selected_risk,
            task_type=task.task_type,
            complexity=task.complexity,
            available_capabilities={"plan", "ai_execute", "ai_validate", "security_review"},
            permissions=self.settings.ai_permissions,
            recommended_mode=mode,
            required_approval=task.approval_required,
            operator_override=True,
        )
        if decision.mode != mode:
            raise NexusError(
                ErrorCode.INVALID_TRANSITION,
                f"Policy does not allow {mode.value} mode for this task: {decision.reason}",
                status_code=409,
            )

        agents = [agent for agent in task.assigned_agents if agent in decision.allowed_workers]
        if mode == ExecutionMode.HUMAN:
            agents = [agent for agent in agents if agent not in {"coder", "validator"}]
        else:
            for required in ("coder", "validator"):
                if required not in agents:
                    agents.append(required)
        security_review_required = bool(
            task.execution_plan.security_review_required or decision.security_review_required
        )
        approval_required = bool(task.approval_required or decision.approval_required)
        if (
            mode != ExecutionMode.HUMAN
            and security_review_required
            and "security_reviewer" not in decision.allowed_workers
        ):
            raise NexusError(
                ErrorCode.INVALID_TRANSITION,
                "This task requires a security reviewer, but that capability is unavailable.",
                status_code=409,
            )
        if (
            security_review_required
            and "security_reviewer" in decision.allowed_workers
            and "security_reviewer" not in agents
        ):
            agents.append("security_reviewer")
        if not agents and "planner" in decision.allowed_workers:
            agents.append("planner")

        target_status = TaskStatus.HUMAN_QUEUE if mode == ExecutionMode.HUMAN else TaskStatus.READY
        now = datetime.now(timezone.utc)
        if now <= task.updated_at:
            now = task.updated_at + timedelta(microseconds=1)
        old_mode = task.mode.value if task.mode else "unset"
        changes = []
        if risk_changed:
            changes.append(
                f"Risk changed from {task.risk.value} to {selected_risk.value}. "
                f"Operator reason: {risk_reason.strip()}"
            )
        if mode_changed:
            changes.append(f"Execution mode changed from {old_mode} to {mode.value}.")
        reason = " ".join(changes)
        history = task.transition_history
        if target_status != task.status:
            history = [
                *history,
                TransitionRecord(
                    from_status=task.status,
                    to_status=target_status,
                    actor=actor,
                    reason=reason,
                    at=now,
                ),
            ]
        plan = task.execution_plan.model_copy(
            update={
                "mode": mode,
                "risk": selected_risk,
                "assigned_agents": agents,
                "policy_reason": decision.reason,
                "approval_required": approval_required,
                "security_review_required": security_review_required,
                "operator_override": True,
            }
        )
        updated = task.model_copy(
            update={
                "mode": mode,
                "risk": selected_risk,
                "assigned_agents": agents,
                "execution_plan": plan,
                "approval_required": approval_required,
                "status": target_status,
                "updated_at": now,
                "transition_history": history,
            }
        )
        try:
            updated = await self.tasks.replace(updated, expected_updated_at=task.updated_at)
        except (RuntimeError, KeyError) as exc:
            raise NexusError(
                ErrorCode.TASK_CONFLICT, "Task changed during this request.", status_code=409
            ) from exc

        await self.records.activity_events.create(
            ActivityEvent(
                id=uuid4().hex,
                task_id=task.id,
                worker_name=actor,
                event_type="task.policy_changed",
                message=reason,
                occurred_at=now,
                source=ActivitySource.SYSTEM,
                metadata={
                    "from_mode": task.mode.value if task.mode else None,
                    "mode": mode.value,
                    "from_risk": task.risk.value,
                    "risk": selected_risk.value,
                    "risk_reason": risk_reason.strip() if risk_changed and risk_reason else None,
                },
            )
        )
        return updated


__all__ = ["TaskModeService"]
