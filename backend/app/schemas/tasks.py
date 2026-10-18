from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.models.task import (
    Complexity,
    ExecutionMode,
    RiskLevel,
    TaskPriority,
    TaskStatus,
    TaskType,
)
from app.schemas.base import StrictModel

_REPOSITORY_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,149}$")


def _validate_repository_key(value: str) -> str:
    if not _REPOSITORY_KEY.fullmatch(value):
        raise ValueError("repository_key must be a configured safe repository key")
    return value


class CreateTaskRequest(StrictModel):
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=20_000)]
    repository_key: Annotated[str, StringConstraints(min_length=1, max_length=150)]
    task_type: TaskType = TaskType.OTHER
    assignee: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    priority: TaskPriority = TaskPriority.NORMAL
    due_date: date | None = None
    labels: list[Annotated[str, StringConstraints(min_length=1, max_length=50)]] = Field(
        default_factory=list, max_length=20
    )

    @field_validator("labels", mode="before")
    @classmethod
    def normalize_labels(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        normalized = [item.strip() if isinstance(item, str) else item for item in value]
        if any(item == "" for item in normalized):
            raise ValueError("labels must not contain blank items")
        return normalized

    @model_validator(mode="after")
    def validate_nonblank_fields(self) -> CreateTaskRequest:
        for field_name in ("title", "description", "repository_key"):
            value = getattr(self, field_name)
            if not value:
                raise ValueError(f"{field_name} must not be blank")
        _validate_repository_key(self.repository_key)
        return self


class UpdateTaskRequest(StrictModel):
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    description: Annotated[str, StringConstraints(min_length=1, max_length=20_000)] | None = None
    repository_key: Annotated[str, StringConstraints(min_length=1, max_length=150)] | None = None
    task_type: TaskType | None = None
    assignee: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    priority: TaskPriority | None = None
    due_date: date | None = None
    labels: list[Annotated[str, StringConstraints(min_length=1, max_length=50)]] | None = Field(
        default=None, max_length=20
    )

    @field_validator("labels", mode="before")
    @classmethod
    def normalize_labels(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        normalized = [item.strip() if isinstance(item, str) else item for item in value]
        if any(item == "" for item in normalized):
            raise ValueError("labels must not contain blank items")
        return normalized

    @model_validator(mode="after")
    def validate_partial_update(self) -> UpdateTaskRequest:
        if not self.model_fields_set:
            raise ValueError("at least one editable field is required")
        for field_name in self.model_fields_set:
            value = getattr(self, field_name)
            if value is None and field_name not in {"assignee", "due_date"}:
                raise ValueError(f"{field_name} must not be null")
            if isinstance(value, str) and not value:
                raise ValueError(f"{field_name} must not be blank")
        if "repository_key" in self.model_fields_set:
            _validate_repository_key(self.repository_key or "")
        if self.labels is not None and any(not label.strip() for label in self.labels):
            raise ValueError("labels must not contain blank items")
        return self


class TaskTransitionRequest(StrictModel):
    to_status: TaskStatus
    reason: Annotated[str, StringConstraints(min_length=1, max_length=2000)]

    @model_validator(mode="after")
    def restrict_public_destination(self) -> TaskTransitionRequest:
        allowed = {
            TaskStatus.BLOCKED,
            TaskStatus.REJECTED,
            TaskStatus.READY,
            TaskStatus.HUMAN_QUEUE,
            TaskStatus.IN_PROGRESS,
        }
        if self.to_status not in allowed:
            raise ValueError("to_status is not a public transition destination")
        if not self.reason:
            raise ValueError("reason must not be blank")
        return self


class HumanInputRequest(StrictModel):
    answer: Annotated[str, StringConstraints(min_length=1, max_length=4000)]
    follow_up: Literal["more_info", "fix_request"] | None = None

    @model_validator(mode="after")
    def validate_answer(self) -> HumanInputRequest:
        if not self.answer.strip():
            raise ValueError("answer must not be blank")
        return self


class AnalyzeTaskRequest(StrictModel):
    model_id: Annotated[str, StringConstraints(min_length=1, max_length=120)] | None = None


class SetTaskModeRequest(StrictModel):
    mode: ExecutionMode
    risk: RiskLevel | None = None
    risk_reason: Annotated[str, StringConstraints(min_length=1, max_length=1000)] | None = None

    @model_validator(mode="after")
    def require_risk_reason(self) -> SetTaskModeRequest:
        if self.risk is not None and not self.risk_reason:
            raise ValueError("risk_reason is required when changing the risk assessment")
        if self.risk_reason is not None and not self.risk:
            raise ValueError("risk_reason can only be provided with a risk assessment")
        return self


class UpdateExecutionPlanRequest(StrictModel):
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    risk: RiskLevel
    complexity: Complexity
    mode: ExecutionMode
    approval_required: bool
    reason: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    assigned_agents: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]] = Field(
        max_length=12
    )
    steps: list[Annotated[str, StringConstraints(min_length=1, max_length=1000)]] = Field(
        min_length=1, max_length=20
    )
    expected_files_or_areas: list[
        Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    ] = Field(default_factory=list, max_length=30)
    validation_plan: list[Annotated[str, StringConstraints(min_length=1, max_length=1000)]] = Field(
        min_length=1, max_length=20
    )
    potential_concerns: list[Annotated[str, StringConstraints(min_length=1, max_length=1000)]] = (
        Field(default_factory=list, max_length=20)
    )

    @model_validator(mode="after")
    def validate_nonblank_fields(self) -> UpdateExecutionPlanRequest:
        if self.title is not None and not self.title.strip():
            raise ValueError("title must not be blank")
        for name in (
            "reason",
            "steps",
            "expected_files_or_areas",
            "validation_plan",
            "potential_concerns",
        ):
            value = getattr(self, name)
            values = value if isinstance(value, list) else [value]
            if any(not item.strip() for item in values):
                raise ValueError(f"{name} must not contain blank items")
        return self


__all__ = [
    "CreateTaskRequest",
    "HumanInputRequest",
    "TaskTransitionRequest",
    "UpdateExecutionPlanRequest",
    "UpdateTaskRequest",
]
