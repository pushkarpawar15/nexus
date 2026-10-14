from __future__ import annotations


class AgentRegistry:
    """Capabilities exposed to policy; model names never grant a capability."""

    def __init__(self) -> None:
        self._capabilities = {
            "planner": frozenset({"plan"}),
            "coder": frozenset({"edit_files"}),
            "validator": frozenset({"discover_and_run_checks"}),
            "security_reviewer": frozenset({"review_patch"}),
        }

    @property
    def workers(self) -> frozenset[str]:
        return frozenset(self._capabilities)

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(cap for values in self._capabilities.values() for cap in values)


__all__ = ["AgentRegistry"]
