from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from app.schemas.base import Fingerprint, ObjectId, StrictModel, UtcTimestampModel


class FindingSource(str, Enum):
    STATIC_CHECK = "STATIC_CHECK"
    MODEL = "MODEL"


class FindingSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class SecurityStatus(str, Enum):
    PASSED = "PASSED"
    FINDINGS = "FINDINGS"
    BLOCKED = "BLOCKED"


class SecurityFinding(UtcTimestampModel):
    source: FindingSource
    severity: FindingSeverity
    file_path: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None
    line: int | None = Field(default=None, ge=1)
    evidence: Annotated[str, StringConstraints(min_length=1, max_length=5000)]
    explanation: Annotated[str, StringConstraints(min_length=1, max_length=4000)]
    remediation: Annotated[str, StringConstraints(min_length=1, max_length=4000)]
    confirmed: bool

    @model_validator(mode="after")
    def enforce_confirmation_source(self) -> SecurityFinding:
        expected = self.source == FindingSource.STATIC_CHECK
        if self.confirmed != expected:
            raise ValueError("static findings must be confirmed and model findings unconfirmed")
        return self


class ModelSecurityFinding(StrictModel):
    severity: FindingSeverity
    file_path: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None
    line: int | None = Field(default=None, ge=1)
    evidence: Annotated[str, StringConstraints(min_length=1, max_length=5000)]
    explanation: Annotated[str, StringConstraints(min_length=1, max_length=4000)]
    remediation: Annotated[str, StringConstraints(min_length=1, max_length=4000)]


class ModelSecurityOutput(StrictModel):
    findings: list[ModelSecurityFinding] = Field(default_factory=list, max_length=30)
    review_limitations: list[Annotated[str, StringConstraints(min_length=1, max_length=1000)]] = (
        Field(default_factory=list, max_length=12)
    )


class SecurityReview(UtcTimestampModel):
    id: ObjectId
    execution_id: ObjectId
    task_id: ObjectId
    patch_fingerprint: Fingerprint
    overall_status: SecurityStatus
    started_at: datetime
    finished_at: datetime
    findings: list[SecurityFinding] = Field(max_length=50)
    review_limitations: list[Annotated[str, StringConstraints(min_length=1, max_length=1000)]] = (
        Field(default_factory=list, max_length=12)
    )
    summary: Annotated[str, StringConstraints(min_length=1, max_length=2000)]


__all__ = [
    "FindingSeverity",
    "FindingSource",
    "SecurityFinding",
    "ModelSecurityFinding",
    "ModelSecurityOutput",
    "SecurityReview",
    "SecurityStatus",
]
