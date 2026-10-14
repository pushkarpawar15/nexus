from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import Field, StringConstraints

from app.schemas.base import Fingerprint, ObjectId, UtcTimestampModel


class ValidationStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"


class ValidationCommandStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    UNAVAILABLE = "UNAVAILABLE"
    UNEXECUTED = "UNEXECUTED"
    SKIPPED = "SKIPPED"


class ValidationCommandResult(UtcTimestampModel):
    command_id: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    command: Annotated[str, StringConstraints(max_length=500)] | None = None
    status: ValidationCommandStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    tests_passed: int | None = Field(default=None, ge=0)
    tests_failed: int | None = Field(default=None, ge=0)
    tests_skipped: int | None = Field(default=None, ge=0)
    stdout_excerpt: Annotated[str, StringConstraints(max_length=10_000)] = ""
    stderr_excerpt: Annotated[str, StringConstraints(max_length=10_000)] = ""
    error: Annotated[str, StringConstraints(max_length=1000)] | None = None


class ValidationReport(UtcTimestampModel):
    id: ObjectId
    execution_id: ObjectId
    task_id: ObjectId
    patch_fingerprint: Fingerprint
    overall_status: ValidationStatus
    started_at: datetime
    finished_at: datetime
    commands: list[ValidationCommandResult] = Field(max_length=50)
    summary: Annotated[str, StringConstraints(min_length=1, max_length=2000)]


__all__ = [
    "ValidationCommandResult",
    "ValidationCommandStatus",
    "ValidationReport",
    "ValidationStatus",
]
