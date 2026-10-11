from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.schemas.base import Fingerprint, ObjectId, UtcTimestampModel


class PullRequestStatus(str, Enum):
    PREPARING = "PREPARING"
    COMMITTED = "COMMITTED"
    PUSHED = "PUSHED"
    CREATED = "CREATED"
    MERGED = "MERGED"


class PullRequestRecord(UtcTimestampModel):
    id: ObjectId
    task_id: ObjectId
    execution_id: ObjectId
    patch_fingerprint: Fingerprint
    repository_key: Annotated[str, StringConstraints(min_length=1, max_length=150)]
    base_branch: Annotated[str, StringConstraints(min_length=1, max_length=250)]
    branch_name: Annotated[str, StringConstraints(min_length=1, max_length=250)]
    status: PullRequestStatus
    created_at: datetime
    updated_at: datetime
    commit_sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{7,64}$")] | None = None
    pr_number: int | None = Field(default=None, ge=1)
    pr_url: Annotated[str, StringConstraints(max_length=500)] | None = None
    merge_commit_sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{7,64}$")] | None = (
        None
    )
    merged_at: datetime | None = None
    error_details: Annotated[str, StringConstraints(max_length=2000)] | None = None

    @field_validator("pr_url")
    @classmethod
    def validate_pr_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"https://github\.com/[^/]+/[^/]+/pull/[1-9][0-9]*", value):
            raise ValueError("pr_url must be a confirmed GitHub pull request URL")
        return value

    @model_validator(mode="after")
    def enforce_execution_key(self) -> PullRequestRecord:
        if self.id != self.execution_id:
            raise ValueError("pull request record id must equal execution_id")
        if self.status == PullRequestStatus.CREATED and (
            self.pr_number is None or self.pr_url is None
        ):
            raise ValueError("CREATED pull requests require a confirmed number and URL")
        if self.status == PullRequestStatus.MERGED and not all(
            (self.pr_number, self.pr_url, self.merge_commit_sha, self.merged_at)
        ):
            raise ValueError("MERGED pull requests require confirmed merge details")
        return self


__all__ = ["PullRequestRecord", "PullRequestStatus"]
