from __future__ import annotations

from dataclasses import dataclass

from app.models.task import Complexity, ExecutionMode, RiskLevel, TaskType


@dataclass(frozen=True)
class WorkforceDecision:
    mode: ExecutionMode
    allowed_workers: tuple[str, ...]
    approval_required: bool
    security_review_required: bool
    reason: str


class WorkforcePolicy:
    """Deterministic authority boundary around model recommendations."""

    worker_order = ("planner", "coder", "validator", "security_reviewer")
    capabilities = {
        "planner": "plan",
        "coder": "ai_execute",
        "validator": "ai_validate",
        "security_reviewer": "security_review",
    }

    def evaluate(
        self,
        *,
        risk: RiskLevel,
        task_type: TaskType,
        complexity: Complexity,
        available_capabilities: set[str] | frozenset[str],
        permissions: set[str] | frozenset[str],
        recommended_mode: ExecutionMode | None = None,
        required_approval: bool = False,
        operator_override: bool = False,
    ) -> WorkforceDecision:
        available = set(available_capabilities)
        permissions = set(permissions)
        workers = tuple(
            worker
            for worker in self.worker_order
            if self.capabilities[worker] in available
            and (worker == "planner" or self.capabilities[worker] in permissions)
        )
        can_code = "coder" in workers and "validator" in workers
        if not can_code:
            workers = tuple(worker for worker in workers if worker not in {"coder", "validator"})
        security_required = risk in {RiskLevel.HIGH, RiskLevel.CRITICAL} or task_type in {
            TaskType.SECURITY,
            TaskType.INFRASTRUCTURE,
        }
        approval = required_approval or security_required

        if operator_override and recommended_mode is not None:
            mode = recommended_mode
            reason = "Execution mode explicitly selected by an operator."
            if mode in {ExecutionMode.AI, ExecutionMode.HYBRID} and not can_code:
                mode = ExecutionMode.HUMAN
                reason = "Coder and validator capabilities are required for AI or hybrid mode."
        elif risk == RiskLevel.CRITICAL:
            mode = ExecutionMode.HUMAN
            workers = tuple(worker for worker in workers if worker != "coder")
            reason = "Critical risk requires human-led work."
        elif security_required:
            mode = ExecutionMode.HYBRID if can_code else ExecutionMode.HUMAN
            reason = "High risk or security-sensitive work requires review."
        elif risk == RiskLevel.MEDIUM:
            mode = ExecutionMode.HYBRID if can_code else ExecutionMode.HUMAN
            if "medium_skip_approval" not in permissions:
                approval = True
            reason = "Medium risk requires a human decision unless explicitly waived."
        elif (
            risk == RiskLevel.LOW
            and task_type
            in {
                TaskType.BUGFIX,
                TaskType.DOCS,
                TaskType.REFACTOR,
                TaskType.TEST,
            }
            and can_code
        ):
            mode = (
                ExecutionMode.HYBRID
                if complexity == Complexity.HIGH or recommended_mode == ExecutionMode.HYBRID
                else ExecutionMode.AI
            )
            reason = "Low-risk bounded work may use the AI worker."
        else:
            mode = ExecutionMode.HUMAN
            reason = "This task does not qualify for autonomous coding."

        if recommended_mode == ExecutionMode.HUMAN:
            mode = ExecutionMode.HUMAN
            reason += " The analysis recommendation narrowed autonomy to human work."
        elif recommended_mode == ExecutionMode.HYBRID and mode == ExecutionMode.AI:
            mode = ExecutionMode.HYBRID

        if mode == ExecutionMode.AI and not can_code:
            mode = ExecutionMode.HUMAN
            reason = "Coding and validation permissions are both required for AI work."
        if security_required and "security_reviewer" not in workers:
            mode = ExecutionMode.HUMAN
            reason += " Security review capability is unavailable."
        return WorkforceDecision(mode, workers, approval, security_required, reason)
