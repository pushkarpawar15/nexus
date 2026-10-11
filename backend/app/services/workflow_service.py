from __future__ import annotations

from datetime import datetime, timezone

from app.core.errors import ErrorCode, NexusError
from app.models.task import (
    ExecutionMode,
    RiskLevel,
    Task,
    TaskStatus,
    TransitionEvidence,
    TransitionRecord,
)
from app.repositories.tasks import TaskRepository

LEGAL_EDGES = {
    TaskStatus.BACKLOG: {TaskStatus.ANALYZING, TaskStatus.BLOCKED},
    TaskStatus.ANALYZING: {
        TaskStatus.TRIAGED,
        TaskStatus.AWAITING_INPUT,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
    },
    TaskStatus.TRIAGED: {TaskStatus.READY, TaskStatus.HUMAN_QUEUE, TaskStatus.BLOCKED},
    TaskStatus.READY: {TaskStatus.ANALYZING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED},
    TaskStatus.HUMAN_QUEUE: {TaskStatus.ANALYZING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED},
    TaskStatus.IN_PROGRESS: {
        TaskStatus.VALIDATING,
        TaskStatus.AWAITING_INPUT,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
    },
    TaskStatus.VALIDATING: {
        TaskStatus.SECURITY_REVIEW,
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.APPROVED,
        TaskStatus.AWAITING_INPUT,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
    },
    TaskStatus.SECURITY_REVIEW: {
        TaskStatus.VALIDATING,
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.AWAITING_INPUT,
        TaskStatus.REJECTED,
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
    },
    TaskStatus.AWAITING_APPROVAL: {TaskStatus.APPROVED, TaskStatus.REJECTED, TaskStatus.BLOCKED},
    TaskStatus.AWAITING_INPUT: {
        TaskStatus.ANALYZING,
        TaskStatus.READY,
        TaskStatus.VALIDATING,
        TaskStatus.SECURITY_REVIEW,
        TaskStatus.BLOCKED,
    },
    TaskStatus.APPROVED: {TaskStatus.PR_CREATED, TaskStatus.BLOCKED},
    TaskStatus.REJECTED: {TaskStatus.TRIAGED},
    TaskStatus.PR_CREATED: {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: {
        TaskStatus.ANALYZING,
        TaskStatus.READY,
        TaskStatus.HUMAN_QUEUE,
        TaskStatus.BLOCKED,
    },
    TaskStatus.BLOCKED: {
        TaskStatus.ANALYZING,
        TaskStatus.READY,
        TaskStatus.HUMAN_QUEUE,
        TaskStatus.TRIAGED,
    },
}


class WorkflowService:
    def __init__(self, tasks: TaskRepository) -> None:
        self.tasks = tasks

    async def transition(
        self,
        task: Task,
        to_status: TaskStatus,
        *,
        actor: str,
        reason: str,
        evidence: TransitionEvidence | None = None,
        trusted: bool = False,
        updates: dict | None = None,
    ) -> Task:
        if to_status not in LEGAL_EDGES[task.status]:
            raise NexusError(
                ErrorCode.INVALID_TRANSITION,
                f"Transition from {task.status.value} to {to_status.value} is not allowed.",
                status_code=409,
            )
        if not trusted and to_status not in {
            TaskStatus.BLOCKED,
            TaskStatus.REJECTED,
            TaskStatus.READY,
            TaskStatus.HUMAN_QUEUE,
            TaskStatus.IN_PROGRESS,
        }:
            raise NexusError(
                ErrorCode.INVALID_TRANSITION, "That destination is service-owned.", status_code=409
            )
        if to_status == TaskStatus.TRIAGED and not all(
            (task.risk, task.complexity, task.mode, task.execution_plan)
        ):
            raise NexusError(
                ErrorCode.INVALID_TRANSITION, "Analysis evidence is required.", status_code=409
            )
        if to_status == TaskStatus.READY and task.mode not in {
            ExecutionMode.AI,
            ExecutionMode.HYBRID,
        }:
            raise NexusError(
                ErrorCode.INVALID_TRANSITION,
                "READY requires an AI or hybrid plan.",
                status_code=409,
            )
        if to_status == TaskStatus.HUMAN_QUEUE and task.mode != ExecutionMode.HUMAN:
            raise NexusError(
                ErrorCode.INVALID_TRANSITION, "HUMAN_QUEUE requires a human plan.", status_code=409
            )
        required_evidence = {
            TaskStatus.VALIDATING: "EXECUTION",
            TaskStatus.SECURITY_REVIEW: "VALIDATION",
            TaskStatus.AWAITING_APPROVAL: "SECURITY_REVIEW"
            if task.status == TaskStatus.SECURITY_REVIEW
            else "VALIDATION",
            TaskStatus.APPROVED: "HUMAN_APPROVAL"
            if task.status == TaskStatus.AWAITING_APPROVAL
            else "VALIDATION",
            TaskStatus.PR_CREATED: "GITHUB_PR",
            TaskStatus.COMPLETED: "COMPLETION",
        }.get(to_status)
        if required_evidence and (evidence is None or evidence.kind.value != required_evidence):
            raise NexusError(
                ErrorCode.INVALID_TRANSITION,
                f"{required_evidence} evidence is required.",
                status_code=409,
            )
        if to_status == TaskStatus.IN_PROGRESS and task.risk == RiskLevel.CRITICAL:
            raise NexusError(
                ErrorCode.INVALID_TRANSITION,
                "Critical work must remain human-led.",
                status_code=409,
            )
        security_required = bool(
            task.risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}
            or task.task_type.value in {"SECURITY", "INFRASTRUCTURE"}
            or (task.execution_plan and task.execution_plan.security_review_required)
        )
        approval_required = bool(task.approval_required or security_required)
        if to_status == TaskStatus.SECURITY_REVIEW and not security_required:
            raise NexusError(
                ErrorCode.INVALID_TRANSITION,
                "This task does not require a separate security-review state.",
                status_code=409,
            )
        if task.status == TaskStatus.VALIDATING:
            if to_status == TaskStatus.AWAITING_APPROVAL and security_required:
                raise NexusError(
                    ErrorCode.INVALID_TRANSITION,
                    "Required security review cannot be bypassed.",
                    status_code=409,
                )
            if to_status == TaskStatus.APPROVED and (security_required or approval_required):
                raise NexusError(
                    ErrorCode.INVALID_TRANSITION,
                    "Required review or human approval cannot be bypassed.",
                    status_code=409,
                )
        if task.status == TaskStatus.SECURITY_REVIEW and to_status == TaskStatus.APPROVED:
            raise NexusError(
                ErrorCode.INVALID_TRANSITION,
                "Required human approval must be recorded before approval.",
                status_code=409,
            )
        if to_status == TaskStatus.PR_CREATED:
            if security_required and not any(
                item.to_status == TaskStatus.SECURITY_REVIEW for item in task.transition_history
            ):
                raise NexusError(
                    ErrorCode.INVALID_TRANSITION,
                    "Required security-review history is missing.",
                    status_code=409,
                )
            if approval_required and not any(
                item.to_status == TaskStatus.APPROVED
                and item.evidence is not None
                and item.evidence.kind.value == "HUMAN_APPROVAL"
                for item in task.transition_history
            ):
                raise NexusError(
                    ErrorCode.INVALID_TRANSITION,
                    "Required human approval history is missing.",
                    status_code=409,
                )
        now = datetime.now(timezone.utc)
        if now <= task.updated_at:
            from datetime import timedelta

            now = task.updated_at + timedelta(microseconds=1)
        transition = TransitionRecord(
            from_status=task.status,
            to_status=to_status,
            actor=actor,
            reason=reason.strip(),
            at=now,
            evidence=evidence,
        )
        updated = task.model_copy(
            update={
                "status": to_status,
                "updated_at": now,
                "transition_history": [*task.transition_history, transition],
                **(updates or {}),
            }
        )
        try:
            return await self.tasks.replace(updated, expected_updated_at=task.updated_at)
        except (RuntimeError, KeyError) as exc:
            raise NexusError(
                ErrorCode.TASK_CONFLICT, "Task changed during this request.", status_code=409
            ) from exc
