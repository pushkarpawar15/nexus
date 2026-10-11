from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class WorkflowStatus(StringEnum):
    BACKLOG = "BACKLOG"
    ANALYZING = "ANALYZING"
    TRIAGED = "TRIAGED"
    READY = "READY"
    HUMAN_QUEUE = "HUMAN_QUEUE"
    IN_PROGRESS = "IN_PROGRESS"
    VALIDATING = "VALIDATING"
    SECURITY_REVIEW = "SECURITY_REVIEW"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    PR_CREATED = "PR_CREATED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class RiskLevel(StringEnum):
    UNASSESSED = "UNASSESSED"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Complexity(StringEnum):
    UNASSESSED = "UNASSESSED"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ExecutionMode(StringEnum):
    AI = "AI"
    HUMAN = "HUMAN"
    HYBRID = "HYBRID"


class ExecutionStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    agent: str | None = Field(default=None, max_length=100)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=30)


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(default="Plan pending analysis", max_length=2000)
    steps: list[ExecutionStep] = Field(default_factory=list, max_length=100)


class TaskCreate(BaseModel):
    """Client-supplied task data. Workflow state and approval are server-owned."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10000)
    repository_key: str = Field(min_length=1, max_length=150)
    risk: RiskLevel = RiskLevel.UNASSESSED
    complexity: Complexity = Complexity.UNASSESSED
    execution_mode: ExecutionMode = ExecutionMode.AI
    assigned_agents: list[str] = Field(default_factory=list, max_length=30)
    execution_plan: ExecutionPlan = Field(default_factory=ExecutionPlan)

    @field_validator("title", "repository_key")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class TaskPatch(BaseModel):
    """Only descriptive fields are mutable through PATCH; state is never accepted."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    def changes(self) -> dict[str, Any]:
        changes = self.model_dump(exclude_unset=True)
        if not changes:
            raise ValueError("PATCH must include title or description")
        if any(value is None for value in changes.values()):
            raise ValueError("PATCH fields cannot be null")
        return changes


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str
    description: str
    repository_key: str
    status: WorkflowStatus = WorkflowStatus.BACKLOG
    risk: RiskLevel = RiskLevel.UNASSESSED
    complexity: Complexity = Complexity.UNASSESSED
    execution_mode: ExecutionMode = ExecutionMode.AI
    assigned_agents: list[str] = Field(default_factory=list)
    execution_plan: ExecutionPlan = Field(default_factory=ExecutionPlan)
    approval_required: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("created_at", "updated_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class WorkflowCommand(StringEnum):
    START_ANALYSIS = "START_ANALYSIS"
    COMPLETE_ANALYSIS = "COMPLETE_ANALYSIS"
    APPLY_POLICY = "APPLY_POLICY"
    ASSIGN_HUMAN = "ASSIGN_HUMAN"
    START_WORK = "START_WORK"
    SUBMIT_FOR_VALIDATION = "SUBMIT_FOR_VALIDATION"
    VALIDATION_PASSED = "VALIDATION_PASSED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    SECURITY_PASSED = "SECURITY_PASSED"
    SECURITY_FAILED = "SECURITY_FAILED"
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    CREATE_PR = "CREATE_PR"
    BLOCK = "BLOCK"
    FAIL = "FAIL"
    RESUME = "RESUME"
    RETRY = "RETRY"


class TriageAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk: RiskLevel
    complexity: Complexity
    execution_plan: ExecutionPlan | None = None
    assigned_agents: list[str] = Field(default_factory=list, max_length=30)


class TransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: WorkflowCommand
    actor: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=2000)
    triage: TriageAssessment | None = None
    assigned_agents: list[str] | None = Field(default=None, max_length=30)
    checks_passed: bool | None = None
    check_run_id: str | None = Field(default=None, max_length=300)
    security_review_id: str | None = Field(default=None, max_length=300)
    pull_request_url: HttpUrl | None = None

    @field_validator("actor")
    @classmethod
    def strip_actor(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("actor must not be blank")
        return value


class TransitionOutcome(StringEnum):
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    CONFLICT = "CONFLICT"


class WorkflowEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    task_id: str
    from_status: WorkflowStatus | None
    to_status: WorkflowStatus | None = None
    command: WorkflowCommand
    outcome: TransitionOutcome
    actor: str
    reason: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    details: dict[str, Any] = Field(default_factory=dict)


class TaskPage(BaseModel):
    items: list[Task]
    limit: int
    offset: int
    has_more: bool
