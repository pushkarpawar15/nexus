from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from app.core.errors import ErrorCode, NexusError
from app.models.task import TaskStatus, TransitionEvidence, TransitionEvidenceKind
from app.repositories.records import WorkflowRepositories
from app.repositories.tasks import TaskRepository
from app.schemas.approval import ApprovalRecord, ApprovalRequest, ApprovalStatus
from app.schemas.execution import ActivityEvent, ActivitySource
from app.services.publication_gates import PublicationGate
from app.services.workflow_service import WorkflowService


class ApprovalService:
    def __init__(self, settings, tasks: TaskRepository, records: WorkflowRepositories) -> None:
        self.settings, self.tasks, self.records = settings, tasks, records
        self.gate = PublicationGate(settings, tasks, records)

    async def decide(
        self, task_id: str, execution_id: str, user_id: str, request: ApprovalRequest
    ) -> ApprovalRecord:
        task = await self.tasks.get(task_id)
        execution = await self.records.executions.get(execution_id)
        if task is None:
            raise NexusError(ErrorCode.TASK_NOT_FOUND, "Task was not found.", status_code=404)
        if execution is None or execution.task_id != task_id:
            raise NexusError(
                ErrorCode.EXECUTION_NOT_FOUND, "Execution was not found.", status_code=404
            )
        fingerprint = execution.patch_fingerprint
        if not fingerprint:
            raise NexusError(
                ErrorCode.PUBLICATION_BLOCKED, "Patch fingerprint is missing.", status_code=409
            )
        decision_id = (
            "approval-" + hashlib.sha256(f"{execution_id}:{fingerprint}".encode()).hexdigest()
        )
        requirements = ["mandatory_validation", "human_approval"]
        if task.execution_plan and task.execution_plan.security_review_required:
            requirements.append("security_review")
        record = ApprovalRecord(
            id=decision_id,
            task_id=task_id,
            execution_id=execution_id,
            patch_fingerprint=fingerprint,
            status=request.status,
            approver_id=user_id,
            decided_at=datetime.now(timezone.utc),
            reason=request.reason,
            policy_requirements=requirements,
        )
        existing = await self.records.approvals.get(decision_id)
        if existing and (
            existing.status != request.status
            or existing.approver_id != user_id
            or existing.reason != request.reason
        ):
            raise NexusError(
                ErrorCode.APPROVAL_CONFLICT,
                "A different decision already exists for this patch.",
                status_code=409,
            )
        if not task.approval_required:
            raise NexusError(
                ErrorCode.APPROVAL_CONFLICT,
                "This task does not require a human approval decision.",
                status_code=409,
            )
        if task.status != TaskStatus.AWAITING_APPROVAL and existing is None:
            raise NexusError(
                ErrorCode.APPROVAL_CONFLICT,
                "Task is not awaiting a required approval.",
                status_code=409,
            )
        eligible, reason = await self.gate.check(task_id, execution_id)
        if not eligible:
            raise NexusError(
                ErrorCode.PUBLICATION_BLOCKED, reason or "Evidence gate failed.", status_code=409
            )
        if existing is None:
            record = await self.records.approvals.create_or_get(record)
        else:
            record = existing
        destination = (
            TaskStatus.APPROVED
            if request.status == ApprovalStatus.APPROVED
            else TaskStatus.REJECTED
        )
        if task.status == TaskStatus.AWAITING_APPROVAL:
            await WorkflowService(self.tasks).transition(
                task,
                destination,
                actor=user_id,
                reason=request.reason,
                evidence=TransitionEvidence(
                    kind=TransitionEvidenceKind.HUMAN_APPROVAL, reference=record.id
                ),
                trusted=True,
            )
        elif task.status != destination or not any(
            transition.to_status == destination
            and transition.evidence is not None
            and transition.evidence.reference == record.id
            for transition in task.transition_history
        ):
            raise NexusError(
                ErrorCode.APPROVAL_CONFLICT,
                "Task state does not match the stored approval decision.",
                status_code=409,
            )
        await self.records.activity_events.create_or_get(
            ActivityEvent(
                id=decision_id,
                task_id=task_id,
                execution_id=execution_id,
                worker_name=user_id,
                event_type=f"approval.{request.status.value.lower()}",
                message=request.reason,
                occurred_at=record.decided_at,
                source=ActivitySource.SYSTEM,
                metadata={"patch_fingerprint": fingerprint},
            )
        )
        return record
