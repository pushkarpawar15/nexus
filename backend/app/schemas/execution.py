from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Any

from pydantic import Field, StringConstraints, field_validator

from app.schemas.base import ObjectId, StrictModel, UtcTimestampModel


class ExecutionStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"


class ActivitySource(str, Enum):
    SYSTEM = "SYSTEM"
    WORKER = "WORKER"
    DEVELOPMENT_FIXTURE = "DEVELOPMENT_FIXTURE"


_SENSITIVE_KEY = re.compile(
    r"(?:authorization|token|secret|password|credential|private[_-]?key|api[_-]?key)",
    re.IGNORECASE,
)


def _safe_metadata(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return value[:1500]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        return [_safe_metadata(item, depth + 1) for item in value[:30]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:30]:
            safe_key = str(key)[:100]
            result[safe_key] = (
                "[REDACTED]" if _SENSITIVE_KEY.search(safe_key) else _safe_metadata(item, depth + 1)
            )
        return result
    raise ValueError("metadata values must be JSON-compatible")


class ExecutionRecord(UtcTimestampModel):
    id: ObjectId
    task_id: ObjectId
    assigned_worker: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    status: ExecutionStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    workspace_reference: Annotated[str, StringConstraints(max_length=1000)] | None = None
    branch_reference: Annotated[str, StringConstraints(max_length=300)] | None = None
    repository_key: Annotated[str, StringConstraints(min_length=1, max_length=150)] | None = None
    model_id: Annotated[str, StringConstraints(min_length=1, max_length=120)] | None = None
    base_commit_sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{7,64}$")] | None = None
    patch_fingerprint: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None
    patch_summary: Annotated[str, StringConstraints(max_length=4000)] | None = None
    changed_files: list[Annotated[str, StringConstraints(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=200
    )
    test_summary: Annotated[str, StringConstraints(max_length=4000)] | None = None
    security_summary: Annotated[str, StringConstraints(max_length=4000)] | None = None
    error_details: Annotated[str, StringConstraints(max_length=2000)] | None = None
    final_result: Annotated[str, StringConstraints(max_length=2000)] | None = None


class ExecutionModelRequest(StrictModel):
    model_id: Annotated[str, StringConstraints(min_length=1, max_length=120)] | None = None


class ActivityEvent(UtcTimestampModel):
    id: ObjectId
    task_id: ObjectId
    execution_id: ObjectId | None = None
    worker_name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    event_type: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    message: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    occurred_at: datetime
    source: ActivitySource = ActivitySource.SYSTEM
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata", mode="before")
    @classmethod
    def sanitize_metadata(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("metadata must be an object")
        return _safe_metadata(value)


__all__ = ["ActivityEvent", "ActivitySource", "ExecutionRecord", "ExecutionStatus"]
