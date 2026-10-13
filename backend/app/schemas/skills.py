from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from app.schemas.base import ObjectId, StrictModel, UtcTimestampModel

SkillScope = Literal["global", "repository"]


class SkillRecord(UtcTimestampModel):
    id: ObjectId
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    instructions: Annotated[str, StringConstraints(min_length=1, max_length=20_000)]
    scope: SkillScope
    repository_key: Annotated[str, StringConstraints(min_length=1, max_length=150)] | None = None
    enabled: bool = True
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    created_by: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    updated_by: Annotated[str, StringConstraints(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def validate_scope(self) -> SkillRecord:
        if self.scope == "global" and self.repository_key is not None:
            raise ValueError("global skills must not have a repository key")
        if self.scope == "repository" and self.repository_key is None:
            raise ValueError("repository skills require a repository key")
        if not self.instructions.strip():
            raise ValueError("instructions must not be blank")
        return self


class SkillWriteRequest(StrictModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    instructions: Annotated[str, StringConstraints(min_length=1, max_length=20_000)]
    scope: SkillScope
    repository_key: Annotated[str, StringConstraints(min_length=1, max_length=150)] | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def validate_scope(self) -> SkillWriteRequest:
        if self.scope == "global" and self.repository_key is not None:
            raise ValueError("global skills must not have a repository key")
        if self.scope == "repository" and self.repository_key is None:
            raise ValueError("repository skills require a repository key")
        if not self.instructions.strip():
            raise ValueError("instructions must not be blank")
        return self


__all__ = ["SkillRecord", "SkillScope", "SkillWriteRequest"]
