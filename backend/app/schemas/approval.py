from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints

from app.schemas.base import Fingerprint, ObjectId, StrictModel, UtcTimestampModel


class ApprovalStatus(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ApprovalRequest(StrictModel):
    status: ApprovalStatus
    reason: Annotated[str, StringConstraints(min_length=1, max_length=2000)]


class ApprovalRecord(UtcTimestampModel):
    model_config = ConfigDict(frozen=True)

    id: ObjectId
    task_id: ObjectId
    execution_id: ObjectId
    patch_fingerprint: Fingerprint
    status: ApprovalStatus
    approver_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    decided_at: datetime
    reason: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    policy_requirements: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]] = (
        Field(default_factory=list, max_length=20)
    )


__all__ = ["ApprovalRecord", "ApprovalRequest", "ApprovalStatus"]
