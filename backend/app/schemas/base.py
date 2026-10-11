from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


Fingerprint = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ObjectId = Annotated[str, StringConstraints(min_length=1, max_length=100)]
NonEmptyText = Annotated[str, StringConstraints(min_length=1)]


def normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


class UtcTimestampModel(StrictModel):
    @field_validator(
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "occurred_at",
        "decided_at",
        "at",
        check_fields=False,
    )
    @classmethod
    def normalize_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return normalize_utc(value)


__all__ = ["Fingerprint", "NonEmptyText", "ObjectId", "StrictModel", "UtcTimestampModel"]
