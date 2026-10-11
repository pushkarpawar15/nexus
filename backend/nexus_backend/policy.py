from dataclasses import dataclass

from .models import Complexity, ExecutionMode, RiskLevel


@dataclass(frozen=True)
class PolicyDecision:
    execution_mode: ExecutionMode
    approval_required: bool


class DeterministicRiskPolicy:
    """Small, explicit safety policy. It does not call or infer from an LLM."""

    def evaluate(
        self,
        risk: RiskLevel,
        complexity: Complexity,
        requested_mode: ExecutionMode,
    ) -> PolicyDecision:
        mode = requested_mode

        # Critical work always enters the human queue. High risk or high
        # complexity work cannot remain AI-only.
        if risk == RiskLevel.CRITICAL:
            mode = ExecutionMode.HUMAN
        elif mode == ExecutionMode.AI and (risk == RiskLevel.HIGH or complexity == Complexity.HIGH):
            mode = ExecutionMode.HYBRID

        approval_required = (
            risk in {RiskLevel.UNASSESSED, RiskLevel.HIGH, RiskLevel.CRITICAL}
            or complexity in {Complexity.UNASSESSED, Complexity.HIGH}
            or mode != ExecutionMode.AI
        )
        return PolicyDecision(
            execution_mode=mode,
            approval_required=approval_required,
        )
