from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated
from uuid import uuid4

from pydantic import Field, StringConstraints

from app.schemas.base import StrictModel, UtcTimestampModel


class StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class TaskStatus(StringEnum):
    BACKLOG = "BACKLOG"
    ANALYZING = "ANALYZING"
    TRIAGED = "TRIAGED"
    READY = "READY"
    HUMAN_QUEUE = "HUMAN_QUEUE"
    IN_PROGRESS = "IN_PROGRESS"
    VALIDATING = "VALIDATING"
    SECURITY_REVIEW = "SECURITY_REVIEW"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    AWAITING_INPUT = "AWAITING_INPUT"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PR_CREATED = "PR_CREATED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class RiskLevel(StringEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Complexity(StringEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ExecutionMode(StringEnum):
    AI = "AI"
    HUMAN = "HUMAN"
    HYBRID = "HYBRID"


class TaskType(StringEnum):
    BUGFIX = "BUGFIX"
    FEATURE = "FEATURE"
    REFACTOR = "REFACTOR"
    DOCS = "DOCS"
    TEST = "TEST"
    SECURITY = "SECURITY"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    OTHER = "OTHER"


class TransitionEvidenceKind(StringEnum):
    EXECUTION = "EXECUTION"
    VALIDATION = "VALIDATION"
    SECURITY_REVIEW = "SECURITY_REVIEW"
    HUMAN_APPROVAL = "HUMAN_APPROVAL"
    GITHUB_PR = "GITHUB_PR"
    COMPLETION = "COMPLETION"


class TransitionEvidence(StrictModel):
    kind: TransitionEvidenceKind
    reference: Annotated[str, StringConstraints(min_length=1, max_length=500)]


class TransitionRecord(UtcTimestampModel):
    from_status: TaskStatus | None
    to_status: TaskStatus
    actor: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    at: datetime
    evidence: TransitionEvidence | None = None


class TaskAttachment(UtcTimestampModel):
    id: Annotated[str, StringConstraints(min_length=1, max_length=100)] = Field(
        default_factory=lambda: uuid4().hex
    )
    filename: Annotated[str, StringConstraints(min_length=1, max_length=240)]
    content_type: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    size_bytes: int = Field(gt=0, le=10_000_000)
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    storage_key: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    created_at: datetime
    created_by: Annotated[str, StringConstraints(min_length=1, max_length=200)]


class PlanSource(StringEnum):
    GEMINI = "GEMINI"
    RULES_FALLBACK = "RULES_FALLBACK"
    OPERATOR = "OPERATOR"


_BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=1000)]


class ModelTaskAnalysis(StrictModel):
    risk: RiskLevel
    complexity: Complexity
    mode: ExecutionMode
    assigned_agents: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]] = Field(
        max_length=12
    )
    approval_required: bool
    reason: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    steps: list[_BoundedText] = Field(min_length=1, max_length=20)
    expected_files_or_areas: list[_BoundedText] = Field(default_factory=list, max_length=30)
    validation_plan: list[_BoundedText] = Field(min_length=1, max_length=20)
    potential_concerns: list[_BoundedText] = Field(default_factory=list, max_length=20)


class ExecutionPlan(ModelTaskAnalysis):
    source: PlanSource
    model_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    policy_reason: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    security_review_required: bool
    operator_override: bool = False


class Task(UtcTimestampModel):
    id: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=20_000)]
    repository_key: Annotated[str, StringConstraints(min_length=1, max_length=150)]
    repository_label: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    task_type: TaskType = TaskType.OTHER
    status: TaskStatus = TaskStatus.BACKLOG
    risk: RiskLevel | None = None
    complexity: Complexity | None = None
    mode: ExecutionMode | None = None
    assigned_agents: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=12
    )
    execution_plan: ExecutionPlan | None = None
    approval_required: bool | None = None
    pending_question: Annotated[str, StringConstraints(max_length=2000)] | None = None
    pending_execution_id: Annotated[str, StringConstraints(max_length=100)] | None = None
    human_guidance: Annotated[str, StringConstraints(max_length=8000)] | None = None
    created_at: datetime
    updated_at: datetime
    created_by: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    transition_history: list[TransitionRecord] = Field(default_factory=list, max_length=500)
    attachments: list[TaskAttachment] = Field(default_factory=list, max_length=10)


class TaskPage(StrictModel):
    items: list[Task]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


__all__ = [
    "Complexity",
    "ExecutionMode",
    "ExecutionPlan",
    "ModelTaskAnalysis",
    "PlanSource",
    "RiskLevel",
    "Task",
    "TaskAttachment",
    "TaskPage",
    "TaskStatus",
    "TaskType",
    "TransitionEvidence",
    "TransitionEvidenceKind",
    "TransitionRecord",
]
