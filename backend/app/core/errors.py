from __future__ import annotations

from enum import Enum
from typing import Any

from .logging import current_correlation_id


class ErrorCode(str, Enum):
    TASK_NOT_FOUND = "task_not_found"
    EXECUTION_NOT_FOUND = "execution_not_found"
    REPOSITORY_NOT_ALLOWED = "repository_not_allowed"
    CODING_WORKSPACE_ERROR = "coding_workspace_error"
    VALIDATION_ERROR = "validation_error"
    INVALID_TRANSITION = "invalid_transition"
    TASK_CONFLICT = "task_conflict"
    DELETION_NOT_ALLOWED = "deletion_not_allowed"
    EXECUTION_CONFLICT = "execution_conflict"
    REPORT_CONFLICT = "report_conflict"
    APPROVAL_CONFLICT = "approval_conflict"
    PULL_REQUEST_CONFLICT = "pull_request_conflict"
    PUBLICATION_BLOCKED = "publication_blocked"
    APPROVAL_FORBIDDEN = "approval_forbidden"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    SECURITY_MODEL_UNAVAILABLE = "security_model_unavailable"
    MODEL_RATE_LIMITED = "model_rate_limited"
    MODEL_TIMEOUT = "model_timeout"
    INVALID_MODEL_OUTPUT = "invalid_model_output"
    GITHUB_OPERATION_ERROR = "github_operation_error"
    HTTP_ERROR = "http_error"
    INTERNAL_ERROR = "internal_error"


class NexusError(Exception):
    """An expected API failure with a stable, safe response representation."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        status_code: int = 400,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details

    def to_envelope(self, correlation_id: str | None = None) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": self.details,
            },
            "correlation_id": correlation_id or current_correlation_id(),
        }
