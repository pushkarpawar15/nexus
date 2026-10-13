from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import ValidationError

from app.core.config import RepositoryTarget, Settings
from app.core.errors import ErrorCode, NexusError
from app.core.logging import redact_credentials
from app.integrations.gemini import GeminiAnalyzer, GeminiRateLimited, GeminiUnavailable
from app.models.task import ExecutionPlan, PlanSource, RiskLevel, Task, TaskStatus
from app.policies.workforce import WorkforcePolicy
from app.repositories.records import WorkflowRepositories
from app.repositories.tasks import TaskRepository
from app.schemas.execution import ActivityEvent, ActivitySource
from app.services.model_catalog import validate_model_id
from app.services.workflow_service import WorkflowService


class AnalysisService:
    def __init__(
        self,
        settings: Settings,
        tasks: TaskRepository,
        records: WorkflowRepositories,
        analyzer: GeminiAnalyzer | None = None,
    ) -> None:
        self.settings, self.tasks, self.records = settings, tasks, records
        self.analyzer = analyzer or GeminiAnalyzer(settings)
        self.policy = WorkforcePolicy()

    async def analyze(
        self,
        task: Task,
        actor: str,
        model_id: str | None = None,
        repository_target: RepositoryTarget | None = None,
    ) -> Task:
        model_id = validate_model_id(
            model_id, self.settings.gemini_analysis_model or self.settings.gemini_model
        )
        if task.status not in {
            TaskStatus.BACKLOG,
            TaskStatus.READY,
            TaskStatus.HUMAN_QUEUE,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.AWAITING_INPUT,
        }:
            raise NexusError(
                ErrorCode.INVALID_TRANSITION,
                "Only backlog, failed, or blocked tasks can be analyzed.",
                status_code=409,
            )
        target = repository_target or self.settings.approved_repositories.get(task.repository_key)
        if target is None:
            raise NexusError(
                ErrorCode.REPOSITORY_NOT_ALLOWED, "Repository is not approved.", status_code=422
            )
        workflow = WorkflowService(self.tasks)
        task = await workflow.transition(
            task,
            TaskStatus.ANALYZING,
            actor=actor,
            reason="Analysis started",
            trusted=True,
            updates=(
                {"pending_question": None, "pending_execution_id": None}
                if task.status == TaskStatus.AWAITING_INPUT
                else None
            ),
        )
        await self._event(task, "analysis.started", "Task analysis started.")
        try:
            repair_error = None
            for attempt in range(2):
                try:
                    if isinstance(self.analyzer, GeminiAnalyzer):
                        proposal = await self.analyzer.analyze(
                            task,
                            target,
                            {"planner", "coder", "validator", "security_reviewer"},
                            repair_error=repair_error,
                            model_id=model_id,
                        )
                    else:
                        proposal = await self.analyzer.analyze(
                            task, target, {"planner", "coder", "validator", "security_reviewer"}
                        )
                    if not isinstance(proposal.reason, str):
                        raise ValueError("invalid model output")
                    break
                except (ValidationError, ValueError) as exc:
                    if isinstance(exc, ValidationError):
                        detail = "; ".join(
                            f"{'.'.join(str(part) for part in issue['loc']) or 'plan'}: "
                            f"{issue['msg']}"
                            for issue in exc.errors(include_input=False)[:8]
                        )
                    else:
                        detail = str(exc)
                    repair_error = " ".join(redact_credentials(detail).split())[:1000]
                    await self._event(
                        task,
                        "analysis.proposal.rejected",
                        f"Plan proposal attempt {attempt + 1} was rejected: "
                        f"{repair_error or 'invalid structured output'}. "
                        + (
                            "Requesting one plan repair."
                            if attempt == 0
                            else "No plan repairs remain."
                        ),
                        {"attempt": attempt + 1, "reason": repair_error},
                    )
                    if attempt:
                        raise
        except TimeoutError as exc:
            await self._fail(task, actor, "Analysis timed out.")
            raise NexusError(
                ErrorCode.MODEL_TIMEOUT, "Analysis timed out.", status_code=504
            ) from exc
        except GeminiRateLimited as exc:
            await self._fail(task, actor, "Analysis was rate limited.")
            raise NexusError(
                ErrorCode.MODEL_RATE_LIMITED,
                "Gemini analysis is rate limited. Try again later.",
                status_code=429,
            ) from exc
        except GeminiUnavailable as exc:
            await self._fail(task, actor, "Analysis service unavailable.")
            raise NexusError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "Gemini analysis is unavailable. Check the backend API key or Vertex AI "
                "credentials.",
                status_code=503,
            ) from exc
        except Exception as exc:
            if isinstance(exc, ValidationError):
                detail = "; ".join(
                    f"{'.'.join(str(part) for part in issue['loc']) or 'plan'}: {issue['msg']}"
                    for issue in exc.errors(include_input=False)[:8]
                )
            else:
                detail = str(exc)
            detail = " ".join(redact_credentials(detail).split())[:700]
            failure = f"Analysis returned invalid output: {detail or type(exc).__name__}"
            await self._fail(
                task,
                actor,
                failure,
                needs_input=isinstance(exc, (ValidationError, ValueError)),
            )
            raise NexusError(
                ErrorCode.INVALID_MODEL_OUTPUT,
                failure,
                status_code=502,
            ) from exc

        risk = (
            max((task.risk, proposal.risk), key=lambda value: list(RiskLevel).index(value))
            if task.risk
            else proposal.risk
        )
        if task.task_type.value in {"SECURITY", "INFRASTRUCTURE"} and list(RiskLevel).index(
            risk
        ) < list(RiskLevel).index(RiskLevel.HIGH):
            risk = RiskLevel.HIGH
        decision = self.policy.evaluate(
            risk=risk,
            task_type=task.task_type,
            complexity=proposal.complexity,
            available_capabilities={"plan", "ai_execute", "ai_validate", "security_review"},
            permissions=self.settings.ai_permissions,
            recommended_mode=proposal.mode,
            required_approval=proposal.approval_required or bool(task.approval_required),
        )
        agents = [agent for agent in proposal.assigned_agents if agent in decision.allowed_workers]
        for required in ("coder", "validator") if decision.mode.value in {"AI", "HYBRID"} else ():
            if required not in agents:
                agents.append(required)
        if (
            decision.security_review_required
            and "security_reviewer" in decision.allowed_workers
            and "security_reviewer" not in agents
        ):
            agents.append("security_reviewer")
        if not agents and "planner" in decision.allowed_workers:
            agents.append("planner")
        plan_data = proposal.model_dump()
        plan_data.update(
            {
                "source": PlanSource.GEMINI,
                "model_id": model_id,
                "policy_reason": decision.reason,
                "security_review_required": decision.security_review_required,
                "risk": risk,
                "mode": decision.mode,
                "assigned_agents": agents,
                "approval_required": decision.approval_required,
            }
        )
        plan = ExecutionPlan.model_validate(plan_data)
        analyzed = task.model_copy(
            update={
                "risk": risk,
                "complexity": proposal.complexity,
                "mode": decision.mode,
                "assigned_agents": agents,
                "approval_required": decision.approval_required,
                "execution_plan": plan,
            }
        )
        analyzed = await self.tasks.replace(analyzed, expected_updated_at=task.updated_at)
        analyzed = await workflow.transition(
            analyzed,
            TaskStatus.TRIAGED,
            actor=actor,
            reason="Structured analysis and policy completed",
            trusted=True,
        )
        final_status = (
            TaskStatus.HUMAN_QUEUE if decision.mode.value == "HUMAN" else TaskStatus.READY
        )
        analyzed = await workflow.transition(
            analyzed, final_status, actor=actor, reason=decision.reason, trusted=True
        )
        await self._event(
            analyzed,
            "analysis.completed",
            "Analysis plan and deterministic policy were saved.",
            {
                "risk": risk.value,
                "complexity": proposal.complexity.value,
                "mode": decision.mode.value,
                "approval_required": decision.approval_required,
                "security_review_required": decision.security_review_required,
                "policy_reason": decision.reason,
                "assigned_agents": agents,
            },
        )
        return analyzed

    async def _fail(
        self, task: Task, actor: str, message: str, *, needs_input: bool = False
    ) -> None:
        current = await self.tasks.get(task.id)
        if current and current.status == TaskStatus.ANALYZING:
            try:
                await WorkflowService(self.tasks).transition(
                    current,
                    TaskStatus.AWAITING_INPUT if needs_input else TaskStatus.FAILED,
                    actor=actor,
                    reason=message,
                    trusted=True,
                    updates=(
                        {
                            "pending_question": (
                                "The plan proposal was rejected after its repair attempt. "
                                "Clarify the task scope or expected behavior, then request "
                                "analysis again."
                            ),
                            "pending_execution_id": None,
                        }
                        if needs_input
                        else None
                    ),
                )
            except NexusError:
                pass
        await self._event(
            task,
            "analysis.operator_action_required" if needs_input else "analysis.failed",
            message,
        )

    async def _event(
        self, task: Task, event_type: str, message: str, metadata: dict | None = None
    ) -> None:
        now = datetime.now(timezone.utc)
        await self.records.activity_events.create(
            ActivityEvent(
                id=uuid4().hex,
                task_id=task.id,
                worker_name="planner",
                event_type=event_type,
                message=message,
                occurred_at=now,
                source=ActivitySource.WORKER,
                metadata=metadata or {},
            )
        )
