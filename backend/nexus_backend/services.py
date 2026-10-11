from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import (
    Complexity,
    ExecutionMode,
    RiskLevel,
    Task,
    TaskCreate,
    TaskPage,
    TaskPatch,
    TransitionOutcome,
    TransitionRequest,
    WorkflowCommand,
    WorkflowEvent,
    WorkflowStatus,
)
from .policy import DeterministicRiskPolicy
from .repositories import (
    ConcurrentTransitionError,
    TaskNotFoundError,
    TaskRepository,
)


class RepositoryNotApprovedError(ValueError):
    pass


class InvalidTransitionError(ValueError):
    pass


class WorkflowInputError(ValueError):
    pass


class TaskService:
    def __init__(
        self,
        repository: TaskRepository,
        approved_repository_keys: frozenset[str],
        policy: DeterministicRiskPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.approved_repository_keys = approved_repository_keys
        self.policy = policy or DeterministicRiskPolicy()

    def create(self, request: TaskCreate) -> Task:
        if request.repository_key not in self.approved_repository_keys:
            raise RepositoryNotApprovedError(
                f"repository key '{request.repository_key}' is not approved"
            )
        decision = self.policy.evaluate(
            request.risk,
            request.complexity,
            request.execution_mode,
        )
        now = datetime.now(timezone.utc)
        task = Task(
            title=request.title,
            description=request.description,
            repository_key=request.repository_key,
            risk=request.risk,
            complexity=request.complexity,
            execution_mode=decision.execution_mode,
            assigned_agents=request.assigned_agents,
            execution_plan=request.execution_plan,
            approval_required=decision.approval_required,
            created_at=now,
            updated_at=now,
        )
        return self.repository.create(task)

    def get(self, task_id: str) -> Task:
        task = self.repository.get(task_id)
        if task is None or task.repository_key not in self.approved_repository_keys:
            raise TaskNotFoundError(task_id)
        return task

    def list(
        self,
        *,
        status: WorkflowStatus | None,
        offset: int,
        limit: int,
    ) -> TaskPage:
        items, has_more = self.repository.list(
            repository_keys=self.approved_repository_keys,
            status=status,
            offset=offset,
            limit=limit,
        )
        return TaskPage(items=items, limit=limit, offset=offset, has_more=has_more)

    def patch(self, task_id: str, request: TaskPatch) -> Task:
        task = self.get(task_id)
        try:
            changes = request.changes()
        except ValueError as exc:
            raise WorkflowInputError(str(exc)) from exc
        updated = task.model_copy(
            update={
                **changes,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        return self.repository.update(updated)


class WorkflowService:
    """Validates and persists every workflow transition attempt."""

    def __init__(
        self,
        repository: TaskRepository,
        task_service: TaskService,
    ) -> None:
        self.repository = repository
        self.task_service = task_service

    def transition(self, task_id: str, request: TransitionRequest) -> Task:
        task = self.repository.get(task_id)
        if task is None:
            self.repository.record_event(
                self._event(
                    task_id=task_id,
                    from_status=None,
                    to_status=None,
                    request=request,
                    outcome=TransitionOutcome.REJECTED,
                    details={"error": "task not found"},
                )
            )
            raise TaskNotFoundError(task_id)

        try:
            next_status, updates, details = self._plan(task, request)
        except (InvalidTransitionError, WorkflowInputError) as exc:
            self.repository.record_event(
                self._event(
                    task_id=task_id,
                    from_status=task.status,
                    to_status=None,
                    request=request,
                    outcome=TransitionOutcome.REJECTED,
                    details={"error": str(exc)},
                )
            )
            raise

        now = datetime.now(timezone.utc)
        updated = task.model_copy(
            update={
                **updates,
                "status": next_status,
                "updated_at": now,
            }
        )
        event = self._event(
            task_id=task_id,
            from_status=task.status,
            to_status=next_status,
            request=request,
            outcome=TransitionOutcome.SUCCEEDED,
            details=details,
        )
        try:
            return self.repository.transition(
                expected_status=task.status,
                task=updated,
                event=event,
            )
        except ConcurrentTransitionError as exc:
            self.repository.record_event(
                self._event(
                    task_id=task_id,
                    from_status=task.status,
                    to_status=next_status,
                    request=request,
                    outcome=TransitionOutcome.CONFLICT,
                    details={"error": str(exc)},
                )
            )
            raise

    def _plan(
        self,
        task: Task,
        request: TransitionRequest,
    ) -> tuple[WorkflowStatus, dict[str, Any], dict[str, Any]]:
        command = request.command
        status = task.status
        updates: dict[str, Any] = {}
        details: dict[str, Any] = {}

        if command == WorkflowCommand.BLOCK:
            self._require(
                status
                not in {
                    WorkflowStatus.PR_CREATED,
                    WorkflowStatus.REJECTED,
                    WorkflowStatus.FAILED,
                },
                "this task cannot be blocked from its current status",
            )
            self._require_reason(request, "blocking a task requires a reason")
            return WorkflowStatus.BLOCKED, updates, details

        if command == WorkflowCommand.FAIL:
            self._require(
                status
                not in {
                    WorkflowStatus.PR_CREATED,
                    WorkflowStatus.REJECTED,
                    WorkflowStatus.FAILED,
                },
                "this task cannot be failed from its current status",
            )
            self._require_reason(request, "failing a task requires a reason")
            return WorkflowStatus.FAILED, updates, details

        if command == WorkflowCommand.RESUME:
            self._require(status == WorkflowStatus.BLOCKED, "only blocked tasks can resume")
            self._require_reason(request, "resuming a blocked task requires a reason")
            return WorkflowStatus.READY, updates, details

        if command == WorkflowCommand.RETRY:
            self._require(status == WorkflowStatus.FAILED, "only failed tasks can be retried")
            self._require_reason(request, "retrying a failed task requires a reason")
            return WorkflowStatus.READY, updates, details

        if command == WorkflowCommand.START_ANALYSIS:
            self._require(status == WorkflowStatus.BACKLOG, "analysis starts from BACKLOG")
            return WorkflowStatus.ANALYZING, updates, details

        if command == WorkflowCommand.COMPLETE_ANALYSIS:
            self._require(status == WorkflowStatus.ANALYZING, "analysis must be in progress")
            self._require(request.triage is not None, "analysis completion requires triage data")
            assessment = request.triage
            assert assessment is not None
            self._require(
                assessment.risk != RiskLevel.UNASSESSED
                and assessment.complexity != Complexity.UNASSESSED,
                "triage must assess both risk and complexity",
            )
            decision = self.task_service.policy.evaluate(
                assessment.risk,
                assessment.complexity,
                task.execution_mode,
            )
            updates.update(
                {
                    "risk": assessment.risk,
                    "complexity": assessment.complexity,
                    "execution_mode": decision.execution_mode,
                    "approval_required": decision.approval_required,
                    "assigned_agents": assessment.assigned_agents,
                }
            )
            if assessment.execution_plan is not None:
                updates["execution_plan"] = assessment.execution_plan
            details.update(
                {
                    "risk": assessment.risk.value,
                    "complexity": assessment.complexity.value,
                    "execution_mode": decision.execution_mode.value,
                    "approval_required": decision.approval_required,
                }
            )
            return WorkflowStatus.TRIAGED, updates, details

        if command == WorkflowCommand.APPLY_POLICY:
            self._require(status == WorkflowStatus.TRIAGED, "policy applies after triage")
            destination = (
                WorkflowStatus.HUMAN_QUEUE
                if task.execution_mode == ExecutionMode.HUMAN
                else WorkflowStatus.READY
            )
            details["policy_approved"] = True
            return destination, updates, details

        if command == WorkflowCommand.ASSIGN_HUMAN:
            self._require(status == WorkflowStatus.HUMAN_QUEUE, "task is not in the human queue")
            self._require(
                request.assigned_agents is not None and len(request.assigned_agents) > 0,
                "human assignment requires at least one assigned agent",
            )
            updates["assigned_agents"] = request.assigned_agents
            return WorkflowStatus.READY, updates, details

        if command == WorkflowCommand.START_WORK:
            self._require(status == WorkflowStatus.READY, "work starts from READY")
            return WorkflowStatus.IN_PROGRESS, updates, details

        if command == WorkflowCommand.SUBMIT_FOR_VALIDATION:
            self._require(status == WorkflowStatus.IN_PROGRESS, "task must be in progress")
            return WorkflowStatus.VALIDATING, updates, details

        if command in {
            WorkflowCommand.VALIDATION_PASSED,
            WorkflowCommand.VALIDATION_FAILED,
        }:
            self._require(status == WorkflowStatus.VALIDATING, "task is not being validated")
            self._require(request.checks_passed is not None, "validation result is required")
            self._require(bool(request.check_run_id), "validation requires a check run ID")
            should_pass = command == WorkflowCommand.VALIDATION_PASSED
            self._require(
                request.checks_passed == should_pass,
                f"{command.value} does not match checks_passed",
            )
            details.update(
                {
                    "check_run_id": request.check_run_id,
                    "checks_passed": request.checks_passed,
                }
            )
            return (
                WorkflowStatus.SECURITY_REVIEW if should_pass else WorkflowStatus.FAILED,
                updates,
                details,
            )

        if command in {
            WorkflowCommand.SECURITY_PASSED,
            WorkflowCommand.SECURITY_FAILED,
        }:
            self._require(
                status == WorkflowStatus.SECURITY_REVIEW,
                "task is not in security review",
            )
            self._require(bool(request.security_review_id), "security review ID is required")
            details["security_review_id"] = request.security_review_id
            if command == WorkflowCommand.SECURITY_FAILED:
                self._require_reason(request, "a failed security review requires a reason")
                return WorkflowStatus.BLOCKED, updates, details
            destination = (
                WorkflowStatus.AWAITING_APPROVAL
                if task.approval_required
                else WorkflowStatus.APPROVED
            )
            details["approval_required"] = task.approval_required
            details["policy_approved"] = not task.approval_required
            return destination, updates, details

        if command == WorkflowCommand.APPROVE:
            self._require(
                status == WorkflowStatus.AWAITING_APPROVAL,
                "only tasks awaiting approval can be approved",
            )
            self._require(task.approval_required, "this task does not require human approval")
            return WorkflowStatus.APPROVED, updates, details

        if command == WorkflowCommand.REJECT:
            self._require(
                status == WorkflowStatus.AWAITING_APPROVAL,
                "only tasks awaiting approval can be rejected",
            )
            self._require_reason(request, "rejecting a task requires a reason")
            return WorkflowStatus.REJECTED, updates, details

        if command == WorkflowCommand.CREATE_PR:
            self._require(status == WorkflowStatus.APPROVED, "PR creation requires approval")
            self._require(
                request.pull_request_url is not None,
                "PR creation requires the created pull request URL",
            )
            url = str(request.pull_request_url)
            self._require(
                url.startswith("https://github.com/") and "/pull/" in url,
                "pull request URL must point to a GitHub pull request",
            )
            details["pull_request_url"] = url
            return WorkflowStatus.PR_CREATED, updates, details

        raise InvalidTransitionError(f"unsupported workflow command '{command.value}'")

    @staticmethod
    def _require(condition: bool, message: str) -> None:
        if not condition:
            raise InvalidTransitionError(message)

    @staticmethod
    def _require_reason(request: TransitionRequest, message: str) -> None:
        if not request.reason or not request.reason.strip():
            raise WorkflowInputError(message)

    @staticmethod
    def _event(
        *,
        task_id: str,
        from_status: WorkflowStatus | None,
        to_status: WorkflowStatus | None,
        request: TransitionRequest,
        outcome: TransitionOutcome,
        details: dict[str, Any],
    ) -> WorkflowEvent:
        return WorkflowEvent(
            task_id=task_id,
            from_status=from_status,
            to_status=to_status,
            command=request.command,
            outcome=outcome,
            actor=request.actor,
            reason=request.reason,
            details=details,
        )
