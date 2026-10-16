from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from datetime import datetime, timezone

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import SecretStr

from app.core.config import Settings
from app.core.logging import redact_credentials
from app.integrations.github_app import GitHubAppIntegration
from app.integrations.github_secrets import GitHubSecretStore
from app.models.task import TaskStatus, TransitionEvidence, TransitionEvidenceKind
from app.repositories.github_connections import GitHubConnectionRepository
from app.repositories.records import WorkflowRepositories
from app.repositories.skills import MemorySkillRepository, SkillRepository
from app.repositories.tasks import TaskRepository
from app.schemas.execution import ActivityEvent, ActivitySource, ExecutionStatus
from app.schemas.security import SecurityStatus
from app.schemas.validation import ValidationStatus
from app.services.coding_agent import CodingAgent
from app.services.coding_workspace import (
    CodingWorkspace,
    CodingWorkspaceError,
    cleanup_workspace,
    create_workspace,
)
from app.services.github_pr_service import GitHubPRService
from app.services.langgraph_workflow import build_operator_input_graph
from app.services.security_service import SecurityService
from app.services.skills import SkillService, skill_fingerprint
from app.services.validation_service import ValidationService
from app.services.workflow_service import WorkflowService

logger = logging.getLogger(__name__)
MAX_VALIDATION_REPAIR_ROUNDS = 3


def _validation_evidence(report) -> list[dict[str, object]]:
    return [
        {
            "command_id": command.command_id,
            "command": command.command or "",
            "status": command.status.value,
            "exit_code": command.exit_code,
            "error": redact_credentials(command.error or "")[:1000],
            "stdout_excerpt": redact_credentials(command.stdout_excerpt)[:1500],
            "stderr_excerpt": redact_credentials(command.stderr_excerpt)[:1500],
        }
        for command in report.commands
    ]


def _validation_feedback(report) -> str:
    failures = [command for command in report.commands if command.status.value != "PASSED"]
    if not failures:
        return redact_credentials(f"Validation failed: {report.summary}")[:12_000]

    sections = [f"Validation failed: {report.summary}"]
    # Reserve a fair share for every failed command so a long first log cannot hide
    # diagnostics from later checks from the repair attempt.
    remaining = 12_000 - len(sections[0]) - 2
    per_command = max(128, remaining // len(failures))
    for command in failures:
        header = (
            f"Command {command.command_id}: {command.status.value} (exit code {command.exit_code})."
        )
        details = [header]
        text_budget = max(24, (per_command - len(header) - 48) // 3)
        if command.error:
            details.append(f"Error: {command.error[:text_budget]}")
        if command.command:
            details.append(f"Ran: {command.command[:text_budget]}")
        output_budget = max(0, per_command - sum(len(part) for part in details) - 24)
        each_output = output_budget // 2
        if each_output > 0 and command.stderr_excerpt:
            details.append(f"stderr:\n{command.stderr_excerpt[-each_output:]}")
        if each_output > 0 and command.stdout_excerpt:
            details.append(f"stdout:\n{command.stdout_excerpt[-each_output:]}")
        sections.append("\n".join(details)[:per_command])
    return redact_credentials("\n\n".join(sections))[:12_000]


class LocalExecutionDispatcher:
    def __init__(self, pipeline: "ExecutionPipeline") -> None:
        self.pipeline = pipeline
        self._tasks: dict[str, asyncio.Task] = {}

    async def dispatch(self, execution_id: str) -> bool:
        task = asyncio.create_task(self.pipeline.run(execution_id), name=f"nexus-{execution_id}")
        self._tasks[execution_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(execution_id, None))
        return True

    async def cancel(self, execution_id: str) -> bool:
        task = self._tasks.get(execution_id)
        if task is None or task.done():
            return False
        await self.pipeline.stop_active_processes(execution_id)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def provide_input(self, execution_id: str, answer: str) -> bool:
        future = self.pipeline._input_waiters.get(execution_id)
        if future is None or future.done():
            return False
        graph_result = await self.pipeline._operator_input_graph.ainvoke(
            Command(resume=answer),
            {"configurable": {"thread_id": execution_id}},
        )
        resumed_answer = graph_result.get("answer")
        if not isinstance(resumed_answer, str) or not resumed_answer.strip():
            raise ValueError("LangGraph did not return the operator's answer.")
        future.set_result(resumed_answer)
        return True

    async def discard_input_checkpoint(self, execution_id: str) -> None:
        delete_thread = getattr(self.pipeline._operator_input_checkpointer, "adelete_thread", None)
        if delete_thread is not None:
            try:
                await delete_thread(execution_id)
            except Exception:
                logger.exception(
                    "Could not remove abandoned LangGraph checkpoints (execution_id=%s)",
                    execution_id,
                )

    async def close(self) -> None:
        for execution_id in tuple(self._tasks):
            await self.cancel(execution_id)


class ExecutionPipeline:
    def __init__(
        self,
        settings: Settings,
        tasks: TaskRepository,
        records: WorkflowRepositories,
        *,
        coder=None,
        validator=None,
        security=None,
        attachment_storage=None,
        github_app: GitHubAppIntegration | None = None,
        github_pr_service: GitHubPRService | None = None,
        checkpointer=None,
        skills_repository: SkillRepository | None = None,
        github_connections: GitHubConnectionRepository | None = None,
        github_secret_store: GitHubSecretStore | None = None,
    ) -> None:
        self.settings, self.tasks, self.records = settings, tasks, records
        self.coder = coder or CodingAgent(settings)
        self.validator = validator or ValidationService(settings, tasks, records)
        self.security = security or SecurityService(settings, records)
        self.skill_service = SkillService(skills_repository or MemorySkillRepository())
        self.attachment_storage = attachment_storage
        self.github_app = github_app
        self.github_connections = github_connections
        self.github_secret_store = github_secret_store
        self._execution_settings: dict[str, Settings] = {}
        self._execution_validators: dict[str, ValidationService] = {}
        self._execution_pr_services: dict[str, GitHubPRService] = {}
        self.github_pr_service = github_pr_service or GitHubPRService(
            settings, tasks, records, github_app=github_app
        )
        self._workspace_stop_events: dict[str, threading.Event] = {}
        self._input_waiters: dict[str, asyncio.Future[str]] = {}
        self._operator_input_checkpointer = checkpointer or InMemorySaver()
        self._operator_input_graph = build_operator_input_graph(self._operator_input_checkpointer)

    async def run(self, execution_id: str) -> None:
        execution = await self.records.executions.get(execution_id)
        if execution is None or execution.status != ExecutionStatus.QUEUED:
            return
        task = await self.tasks.get(execution.task_id)
        if task is None or task.status != TaskStatus.IN_PROGRESS:
            reason = "Task changed before worker start."
            await self._finish_execution(execution, ExecutionStatus.BLOCKED, reason, "BLOCKED")
            await self._event(
                execution.task_id,
                execution.id,
                "execution.blocked",
                reason,
            )
            return
        now = datetime.now(timezone.utc)
        running = execution.model_copy(
            update={"status": ExecutionStatus.RUNNING, "started_at": now, "updated_at": now}
        )
        await self.records.executions.replace(running, expected_updated_at=execution.updated_at)
        await self._event(
            task.id,
            execution.id,
            "workspace.started",
            "Preparing an isolated workspace from the approved repository.",
        )
        stage = "workspace setup"
        workspace: CodingWorkspace | None = None
        workspace_stop = threading.Event()
        self._workspace_stop_events[execution.id] = workspace_stop
        workspace_settings = self.settings
        skills = []
        try:
            skills = await self.skill_service.resolve_for_execution(task.repository_key)
            await self._event(
                task.id,
                execution.id,
                "coding.skills_resolved",
                (
                    f"Loaded {len(skills)} enabled skills for this coding execution."
                    if skills
                    else "No enabled global or repository-specific skills matched this execution."
                ),
                {
                    "skills": [
                        {
                            "id": skill.id,
                            "name": skill.name,
                            "version": skill.version,
                            "sha256": skill_fingerprint(skill),
                        }
                        for skill in skills
                    ],
                },
            )
            target = self.settings.approved_repositories.get(task.repository_key)
            if self.github_connections is not None:
                connection = await self.github_connections.get(task.created_by, task.repository_key)
                if connection is None:
                    connections = await self.github_connections.list_all()
                    connection = next(
                        (
                            item
                            for item in connections
                            if item.repository_key == task.repository_key
                        ),
                        None,
                    )
                if connection is not None:
                    target = connection.target
                    if self.github_secret_store is None:
                        raise CodingWorkspaceError("Repository credential storage is unavailable.")
                    token = await self.github_secret_store.get(connection.secret_name)
                    task_targets = dict(self.settings.approved_repositories)
                    task_targets[task.repository_key] = target
                    workspace_settings = self.settings.model_copy(
                        update={"github_token": token, "approved_repositories": task_targets}
                    )
                    self._execution_settings[execution.id] = workspace_settings
                    self._execution_validators[execution.id] = ValidationService(
                        workspace_settings, self.tasks, self.records
                    )
                    self._execution_pr_services[execution.id] = GitHubPRService(
                        workspace_settings,
                        self.tasks,
                        self.records,
                        github_app=self.github_app,
                    )
            if target is None:
                raise CodingWorkspaceError("Configured repository is no longer approved.")
            if target.github_installation_id and self.github_app is not None:
                token = await self.github_app.installation_token(target.github_installation_id)
                workspace_settings = self.settings.model_copy(
                    update={"github_token": SecretStr(token)}
                )
            workspace_job = asyncio.create_task(
                asyncio.to_thread(
                    create_workspace,
                    workspace_settings,
                    target,
                    task.id,
                    execution.id,
                    stop_event=workspace_stop,
                )
            )
            try:
                workspace = await asyncio.shield(workspace_job)
            except asyncio.CancelledError:
                try:
                    unfinished_workspace = await workspace_job
                except Exception:
                    pass
                else:
                    await asyncio.to_thread(
                        cleanup_workspace, workspace_settings, unfinished_workspace
                    )
                raise
            workspace_execution = running.model_copy(
                update={
                    "workspace_reference": str(workspace.path),
                    "branch_reference": workspace.branch,
                    "base_commit_sha": workspace.base_commit_sha,
                    "repository_key": task.repository_key,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            await self.records.executions.replace(
                workspace_execution, expected_updated_at=running.updated_at
            )
            running = workspace_execution
            await self._event(
                task.id,
                execution.id,
                "workspace.ready",
                f"Workspace is ready on branch {workspace.branch}, based on "
                f"{target.base_branch} at {workspace.base_commit_sha[:12]}.",
                {
                    "branch": workspace.branch,
                    "base_branch": target.base_branch,
                    "base_commit_sha": workspace.base_commit_sha,
                },
            )
            attachments = []
            if task.attachments:
                if self.attachment_storage is None:
                    raise CodingWorkspaceError("Task attachment storage is unavailable.")
                for attachment in task.attachments:
                    content = await self.attachment_storage.get(attachment.storage_key)
                    attachments.append((attachment.filename, attachment.content_type, content))

            async def report_coding_progress(
                kind: str, message: str, metadata: dict | None = None
            ) -> None:
                await self._event(task.id, execution.id, kind, message, metadata)

            edit_options = {"model_id": execution.model_id or self.settings.gemini_coding_model}
            if attachments:
                edit_options["attachments"] = attachments
            if isinstance(self.coder, CodingAgent):
                edit_options["on_progress"] = report_coding_progress
                edit_options["skills"] = skills
            await self._event(
                task.id,
                execution.id,
                "coding.started",
                "Starting the configured coding model.",
            )
            if attachments:
                stage = "coding"
                changed_files, fingerprint, patch_summary = await self.coder.edit(
                    task, workspace, **edit_options
                )
            else:
                stage = "coding"
                changed_files, fingerprint, patch_summary = await self.coder.edit(
                    task, workspace, **edit_options
                )
            if not changed_files:
                raise ValueError("Coding stage produced no changed files.")
            updated_execution = running.model_copy(
                update={
                    "workspace_reference": str(workspace.path),
                    "branch_reference": workspace.branch,
                    "base_commit_sha": workspace.base_commit_sha,
                    "patch_fingerprint": fingerprint,
                    "patch_summary": patch_summary,
                    "changed_files": changed_files,
                    "repository_key": task.repository_key,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            await self.records.executions.replace(
                updated_execution, expected_updated_at=running.updated_at
            )
            await self._event(
                task.id,
                execution.id,
                "coding.patch_created",
                "A patch was created from actual workspace changes. It remains on the local "
                "execution branch until GitHub publication commits it.",
                {
                    "files": changed_files,
                    "patch_summary": patch_summary,
                    "patch_fingerprint": fingerprint,
                    "branch": workspace.branch,
                    "base_commit_sha": workspace.base_commit_sha,
                    "commit_created": False,
                },
            )
            task = await WorkflowService(self.tasks).transition(
                task,
                TaskStatus.VALIDATING,
                actor="coder",
                reason="A verified patch is ready for repository validation.",
                evidence=TransitionEvidence(
                    kind=TransitionEvidenceKind.EXECUTION, reference=execution.id
                ),
                trusted=True,
            )
            validated = await self._validate_until_pass(
                task, updated_execution, workspace, edit_options
            )
            if validated is None:
                return
            updated_execution, validation, repair_round = validated
            if await self._finish_skipped_validation(task, updated_execution, validation):
                return
            security_required = bool(
                task.execution_plan and task.execution_plan.security_review_required
            )
            security_repair_round = 0
            while True:
                if security_required and task.status == TaskStatus.VALIDATING:
                    task = await WorkflowService(self.tasks).transition(
                        task,
                        TaskStatus.SECURITY_REVIEW,
                        actor="validator",
                        reason="Validation passed; required security review starts.",
                        evidence=TransitionEvidence(
                            kind=TransitionEvidenceKind.VALIDATION, reference=validation.id
                        ),
                        trusted=True,
                    )
                stage = "security review"
                await self._event(
                    task.id,
                    execution.id,
                    "security.started",
                    "Checking the validated patch for security findings.",
                    {"repair_round": security_repair_round},
                )
                review = await self.security.review(
                    task, updated_execution, workspace.path, validation
                )
                finding_evidence = [
                    {
                        "severity": finding.severity.value,
                        "file_path": finding.file_path,
                        "line": finding.line,
                        "explanation": finding.explanation,
                        "remediation": finding.remediation,
                    }
                    for finding in review.findings
                ]
                await self._event(
                    task.id,
                    execution.id,
                    f"security.{review.overall_status.value.lower()}",
                    review.summary,
                    {"repair_round": security_repair_round, "findings": finding_evidence},
                )
                if review.overall_status == SecurityStatus.PASSED:
                    break

                can_repair_security = (
                    review.overall_status == SecurityStatus.FINDINGS
                    and security_repair_round < 2
                    and repair_round < MAX_VALIDATION_REPAIR_ROUNDS
                )
                if not can_repair_security:
                    reason = review.summary
                    if review.overall_status != SecurityStatus.FINDINGS:
                        current = await self.tasks.get(task.id)
                        await self._event(
                            task.id,
                            execution.id,
                            "security.operator_action_required",
                            reason,
                            {"findings": finding_evidence},
                        )
                        if review.overall_status == SecurityStatus.BLOCKED and current:
                            task, _answer = await self._request_operator_input(
                                current,
                                updated_execution,
                                question=(
                                    "Security review could not complete. Review its reported "
                                    "reason, resolve any configuration issue, then provide "
                                    "guidance to retry the review."
                                ),
                                reason=reason,
                                metadata={"findings": finding_evidence},
                            )
                            task = await WorkflowService(self.tasks).transition(
                                task,
                                TaskStatus.SECURITY_REVIEW,
                                actor="operator",
                                reason="Operator requested another security review.",
                                evidence=TransitionEvidence(
                                    kind=TransitionEvidenceKind.VALIDATION,
                                    reference=validation.id,
                                ),
                                trusted=True,
                            )
                            continue
                        if current and current.status in {
                            TaskStatus.VALIDATING,
                            TaskStatus.SECURITY_REVIEW,
                        }:
                            await WorkflowService(self.tasks).transition(
                                current,
                                TaskStatus.BLOCKED,
                                actor="security_reviewer",
                                reason=reason,
                                trusted=True,
                            )
                        await self._finish_execution(
                            updated_execution, ExecutionStatus.BLOCKED, reason, "BLOCKED"
                        )
                        return
                    reason += " Security repair limit reached; operator input is required."
                    await self._event(
                        task.id,
                        execution.id,
                        "security.operator_action_required",
                        reason,
                        {"findings": finding_evidence},
                    )
                    current = await self.tasks.get(task.id)
                    if current is None:
                        raise ValueError("Task disappeared while waiting for security guidance.")
                    task, human_answer = await self._request_operator_input(
                        current,
                        updated_execution,
                        question=(
                            "Review the security findings and tell the coding agent what to "
                            "change, or explain why a finding is acceptable."
                        ),
                        reason=reason,
                        metadata={"findings": finding_evidence},
                    )
                    security_repair_round = 1
                    repair_round = 1
                    repair_feedback = (
                        "Security findings to fix: "
                        + json.dumps(finding_evidence, ensure_ascii=False)[:10_000]
                        + "\n\nOperator guidance: "
                        + human_answer
                    )
                else:
                    security_repair_round += 1
                    repair_round += 1
                if can_repair_security:
                    repair_feedback = (
                        "Security findings to fix: "
                        + json.dumps(finding_evidence, ensure_ascii=False)[:10_000]
                    )
                await self._event(
                    task.id,
                    execution.id,
                    "coding.security_repair_requested",
                    "Security review found issues. Asking the coding agent to repair the patch "
                    f"(repair {security_repair_round} of 2).",
                    {
                        "repair_round": security_repair_round,
                        "findings": finding_evidence,
                    },
                )
                if task.status == TaskStatus.SECURITY_REVIEW:
                    task = await WorkflowService(self.tasks).transition(
                        task,
                        TaskStatus.VALIDATING,
                        actor="security_reviewer",
                        reason="Security findings require a code repair and fresh validation.",
                        evidence=TransitionEvidence(
                            kind=TransitionEvidenceKind.EXECUTION, reference=execution.id
                        ),
                        trusted=True,
                    )
                stage = "security repair coding"
                repair_options = dict(edit_options)
                repair_options["previous_error"] = repair_feedback
                changed_files, fingerprint, patch_summary = await self.coder.edit(
                    task, workspace, **repair_options
                )
                if not changed_files:
                    raise ValueError("Security repair produced no changed files.")
                repaired_execution = updated_execution.model_copy(
                    update={
                        "patch_fingerprint": fingerprint,
                        "patch_summary": patch_summary,
                        "changed_files": changed_files,
                        "updated_at": datetime.now(timezone.utc),
                    }
                )
                await self.records.executions.replace(
                    repaired_execution, expected_updated_at=updated_execution.updated_at
                )
                updated_execution = repaired_execution
                await self._event(
                    task.id,
                    execution.id,
                    "coding.patch_repaired",
                    f"Applied security repair {security_repair_round}; rerunning validation.",
                    {
                        "repair_round": security_repair_round,
                        "files": changed_files,
                        "patch_summary": patch_summary,
                        "patch_fingerprint": fingerprint,
                        "branch": workspace.branch,
                        "base_commit_sha": workspace.base_commit_sha,
                        "commit_created": False,
                    },
                )
                validated = await self._validate_until_pass(
                    task, updated_execution, workspace, edit_options, repair_round=repair_round
                )
                if validated is None:
                    return
                updated_execution, validation, repair_round = validated
                if await self._finish_skipped_validation(task, updated_execution, validation):
                    return
            current = await self.tasks.get(task.id)
            if current is None:
                raise ValueError("Task disappeared during execution.")
            evidence = TransitionEvidence(
                kind=TransitionEvidenceKind.SECURITY_REVIEW
                if security_required
                else TransitionEvidenceKind.VALIDATION,
                reference=review.id if security_required else validation.id,
            )
            if current.approval_required:
                await WorkflowService(self.tasks).transition(
                    current,
                    TaskStatus.AWAITING_APPROVAL,
                    actor="security_reviewer",
                    reason="Validation and security review passed; human decision required.",
                    evidence=evidence,
                    trusted=True,
                )
            else:
                await WorkflowService(self.tasks).transition(
                    current,
                    TaskStatus.APPROVED,
                    actor="security_reviewer",
                    reason="Required automated gates passed; no human approval was required.",
                    evidence=evidence,
                    trusted=True,
                )
            now = datetime.now(timezone.utc)
            succeeded = updated_execution.model_copy(
                update={
                    "status": ExecutionStatus.SUCCEEDED,
                    "updated_at": now,
                    "finished_at": now,
                    "test_summary": validation.summary,
                    "security_summary": review.summary,
                    "final_result": "PATCH_VALIDATED",
                }
            )
            await self.records.executions.replace(
                succeeded, expected_updated_at=updated_execution.updated_at
            )
            await self._event(
                task.id,
                execution.id,
                "execution.patch_validated",
                "Patch validation and security review completed.",
            )
            if current.approval_required:
                stage = "awaiting approval"
                await self._event(
                    task.id,
                    execution.id,
                    "github.awaiting_approval",
                    "All automated fixes and checks passed. PR publication is waiting for the "
                    "required human approval.",
                    {
                        "branch": updated_execution.branch_reference,
                        "patch_fingerprint": updated_execution.patch_fingerprint,
                        "approval_required": True,
                    },
                )
            elif (
                execution_pr_service := self._execution_pr_services.get(
                    execution.id, self.github_pr_service
                )
            ) is not None:
                stage = "GitHub publication"
                publication_error = None
                for publish_attempt in range(3):
                    try:
                        await execution_pr_service.create(task.id, execution.id)
                        publication_error = None
                        break
                    except Exception as exc:
                        publication_error = exc
                        if publish_attempt == 2:
                            break
                        await self._event(
                            task.id,
                            execution.id,
                            "github.auto_publish_retry",
                            "GitHub publication did not complete; retrying its saved checkpoint "
                            f"(attempt {publish_attempt + 2} of 3).",
                            {
                                "attempt": publish_attempt + 1,
                                "next_attempt": publish_attempt + 2,
                                "reason": " ".join(redact_credentials(str(exc)).split())[:1000],
                            },
                        )
                        await asyncio.sleep(2**publish_attempt)
                if publication_error is not None:
                    exc = publication_error
                    detail = " ".join(redact_credentials(str(exc)).split())[:1000]
                    await self._event(
                        task.id,
                        execution.id,
                        "github.auto_publish_pending",
                        "Validated patch is ready, but PR creation needs a task-page retry. "
                        f"Reason: {detail or type(exc).__name__}",
                        {"reason": detail or type(exc).__name__},
                    )
            else:
                await self._event(
                    task.id,
                    execution.id,
                    "github.auto_publish_unavailable",
                    "All automated fixes and checks passed, but GitHub PR publication is not "
                    "configured for this worker.",
                )
        except asyncio.CancelledError:
            reason = "Local worker stopped before the execution completed."
            if workspace is not None:
                try:
                    await asyncio.to_thread(cleanup_workspace, workspace_settings, workspace)
                except Exception:
                    logger.exception(
                        "Could not clean stopped execution workspace (execution_id=%s)",
                        execution.id,
                    )
            await self._mark_task(task.id, TaskStatus.BLOCKED, reason)
            await self._finish_execution(
                running,
                ExecutionStatus.CANCELLED,
                reason,
                "CANCELLED",
            )
            await self._event(task.id, execution.id, "execution.cancelled", reason)
            raise
        except Exception as exc:
            logger.exception(
                "Execution stage failed (execution_id=%s task_id=%s stage=%s)",
                execution.id,
                task.id,
                stage,
            )
            detail = redact_credentials(str(exc)).strip()
            message = f"{stage.capitalize()} failed: {detail or type(exc).__name__}"[:1000]
            current = await self.tasks.get(task.id)
            if current and current.status in {
                TaskStatus.IN_PROGRESS,
                TaskStatus.VALIDATING,
                TaskStatus.SECURITY_REVIEW,
            }:
                needs_operator_input = stage in {"coding", "security repair coding"}
                destination = (
                    TaskStatus.AWAITING_INPUT
                    if needs_operator_input
                    else TaskStatus.BLOCKED
                    if isinstance(exc, (CodingWorkspaceError, TimeoutError))
                    else TaskStatus.FAILED
                )
                try:
                    await WorkflowService(self.tasks).transition(
                        current,
                        destination,
                        actor="system",
                        reason=message,
                        trusted=True,
                        updates=(
                            {
                                "pending_question": (
                                    "Review the coding proposal or coding error in the activity "
                                    "log and provide guidance for the next attempt."
                                ),
                                "pending_execution_id": execution.id,
                            }
                            if needs_operator_input
                            else None
                        ),
                    )
                except Exception:
                    pass
            await self._finish_execution(
                running,
                ExecutionStatus.BLOCKED
                if isinstance(exc, (CodingWorkspaceError, TimeoutError))
                else ExecutionStatus.FAILED,
                message,
                "BLOCKED" if isinstance(exc, (CodingWorkspaceError, TimeoutError)) else "FAILED",
            )
            await self._event(task.id, execution.id, "execution.failed", message)
        finally:
            self._workspace_stop_events.pop(execution.id, None)
            self._execution_settings.pop(execution.id, None)
            self._execution_validators.pop(execution.id, None)
            self._execution_pr_services.pop(execution.id, None)
            current_execution = await self.records.executions.get(execution.id)
            delete_thread = getattr(self._operator_input_checkpointer, "adelete_thread", None)
            if (
                current_execution
                and current_execution.status
                not in {ExecutionStatus.QUEUED, ExecutionStatus.RUNNING}
                and delete_thread is not None
            ):
                try:
                    await delete_thread(execution.id)
                except Exception:
                    logger.exception(
                        "Could not remove completed LangGraph checkpoints (execution_id=%s)",
                        execution.id,
                    )

    async def _request_operator_input(
        self,
        task,
        execution,
        *,
        question: str,
        reason: str,
        metadata: dict | None = None,
    ):
        future = asyncio.get_running_loop().create_future()
        self._input_waiters[execution.id] = future
        try:
            await self._operator_input_graph.ainvoke(
                {"question": question, "answer": None},
                {"configurable": {"thread_id": execution.id}},
            )
            await WorkflowService(self.tasks).transition(
                task,
                TaskStatus.AWAITING_INPUT,
                actor="workflow",
                reason=reason,
                trusted=True,
                updates={
                    "pending_question": question,
                    "pending_execution_id": execution.id,
                },
            )
            await self._event(
                task.id,
                execution.id,
                "workflow.input_requested",
                question,
                metadata or {},
            )
            answer = await future
            current = await self.tasks.get(task.id)
            if current is None or current.status != TaskStatus.AWAITING_INPUT:
                raise ValueError("Task changed while waiting for operator input.")
            resumed = await WorkflowService(self.tasks).transition(
                current,
                TaskStatus.VALIDATING,
                actor="operator",
                reason="Operator guidance received; continuing the same execution workspace.",
                evidence=TransitionEvidence(
                    kind=TransitionEvidenceKind.EXECUTION, reference=execution.id
                ),
                trusted=True,
                updates={
                    "pending_question": None,
                    "pending_execution_id": None,
                    "human_guidance": current.human_guidance,
                },
            )
            await self._event(
                task.id,
                execution.id,
                "human.input_received",
                "Operator guidance was saved; the same execution is resuming.",
                {"guidance": current.human_guidance or ""},
            )
            return resumed, answer.strip()
        finally:
            self._input_waiters.pop(execution.id, None)

    async def _validate_until_pass(
        self,
        task,
        execution,
        workspace: CodingWorkspace,
        edit_options: dict,
        *,
        repair_round: int = 0,
    ):
        last_failure_fingerprint: str | None = None
        repeated_failure_count = 0
        while True:
            await self._event(
                task.id,
                execution.id,
                "validation.started",
                f"Running Antigravity-discovered repository checks (round {repair_round + 1}).",
                {"round": repair_round + 1},
            )
            validator = self._execution_validators.get(execution.id, self.validator)
            validation = await validator.validate(task, execution, workspace.path)
            command_evidence = _validation_evidence(validation)
            await self._event(
                task.id,
                execution.id,
                f"validation.{validation.overall_status.value.lower()}",
                validation.summary,
                {"round": repair_round + 1, "commands": command_evidence},
            )
            if validation.overall_status == ValidationStatus.PASSED:
                return execution, validation, repair_round
            if validation.overall_status == ValidationStatus.SKIPPED:
                return execution, validation, repair_round
            if validation.overall_status == ValidationStatus.BLOCKED:
                await self._finish_blocked_validation(task, execution, validation)
                return None

            repair_feedback = _validation_feedback(validation)
            failure_fingerprint = hashlib.sha256(repair_feedback.encode("utf-8")).hexdigest()
            if failure_fingerprint == last_failure_fingerprint:
                repeated_failure_count += 1
            else:
                repeated_failure_count = 0
            last_failure_fingerprint = failure_fingerprint
            if repeated_failure_count >= 2:
                reason = (
                    "Validation returned the same failure diagnostics after two coding repairs. "
                    "Stopped automatic retries to prevent repeating an ineffective repair. "
                    + validation.summary
                )[:2000]
                await self._finish_repair_stalled_validation(
                    task,
                    execution,
                    validation,
                    reason=reason,
                    repair_rounds=repair_round,
                )
                return None

            can_repair = (
                validation.overall_status == ValidationStatus.FAILED
                and repair_round < MAX_VALIDATION_REPAIR_ROUNDS
            )
            if not can_repair:
                reason = validation.summary
                reason += (
                    f" Automatic repair limit reached after {repair_round} repair rounds; "
                    "operator input is required."
                )
                await self._event(
                    task.id,
                    execution.id,
                    "validation.operator_action_required",
                    reason,
                    {"commands": command_evidence, "repair_rounds": repair_round},
                )
                task, human_answer = await self._request_operator_input(
                    task,
                    execution,
                    question=(
                        "Review the failed validation commands and provide guidance for the "
                        "coding agent, or explain which environment issue needs fixing."
                    ),
                    reason=reason,
                    metadata={"commands": command_evidence, "repair_rounds": repair_round},
                )
                repair_round = 1
                repair_feedback = _validation_feedback(validation) + (
                    "\n\nOperator guidance: " + human_answer
                )
                repair_options = dict(edit_options)
                repair_options["previous_error"] = repair_feedback
                changed_files, fingerprint, patch_summary = await self.coder.edit(
                    task, workspace, **repair_options
                )
                if not changed_files:
                    raise ValueError("Validation repair produced no changed files.")
                repaired_execution = execution.model_copy(
                    update={
                        "patch_fingerprint": fingerprint,
                        "patch_summary": patch_summary,
                        "changed_files": changed_files,
                        "updated_at": datetime.now(timezone.utc),
                    }
                )
                await self.records.executions.replace(
                    repaired_execution, expected_updated_at=execution.updated_at
                )
                execution = repaired_execution
                await self._event(
                    task.id,
                    execution.id,
                    "coding.patch_repaired",
                    "Applied operator-guided repair in the existing workspace; "
                    "rerunning validation.",
                    {
                        "repair_round": repair_round,
                        "files": changed_files,
                        "patch_summary": patch_summary,
                        "patch_fingerprint": fingerprint,
                        "branch": execution.branch_reference,
                        "base_commit_sha": execution.base_commit_sha,
                        "commit_created": False,
                        "operator_guidance_applied": True,
                    },
                )
                continue

            repair_round += 1
            await self._event(
                task.id,
                execution.id,
                "coding.validation_repair_requested",
                "Validation failed. Asking the coding agent to fix the reported issues "
                f"(repair {repair_round} of {MAX_VALIDATION_REPAIR_ROUNDS}).",
                {
                    "repair_round": repair_round,
                    "maximum_repair_rounds": MAX_VALIDATION_REPAIR_ROUNDS,
                    "validation_summary": validation.summary,
                    "commands": command_evidence,
                },
            )
            repair_options = dict(edit_options)
            repair_options["previous_error"] = repair_feedback
            changed_files, fingerprint, patch_summary = await self.coder.edit(
                task, workspace, **repair_options
            )
            if not changed_files:
                raise ValueError("Validation repair produced no changed files.")
            repaired_execution = execution.model_copy(
                update={
                    "patch_fingerprint": fingerprint,
                    "patch_summary": patch_summary,
                    "changed_files": changed_files,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            await self.records.executions.replace(
                repaired_execution, expected_updated_at=execution.updated_at
            )
            execution = repaired_execution
            await self._event(
                task.id,
                execution.id,
                "coding.patch_repaired",
                f"Applied validation repair {repair_round}; rerunning validation.",
                {
                    "repair_round": repair_round,
                    "files": changed_files,
                    "patch_summary": patch_summary,
                    "patch_fingerprint": fingerprint,
                    "branch": workspace.branch,
                    "base_commit_sha": workspace.base_commit_sha,
                    "commit_created": False,
                },
            )

    async def _finish_skipped_validation(self, task, execution, validation) -> bool:
        if validation.overall_status != ValidationStatus.SKIPPED:
            return False
        reason = validation.summary
        await self._event(
            task.id,
            execution.id,
            "validation.configuration_required",
            reason,
            {"commands": _validation_evidence(validation)},
        )
        current = await self.tasks.get(task.id)
        if current and current.status == TaskStatus.VALIDATING:
            await WorkflowService(self.tasks).transition(
                current,
                TaskStatus.BLOCKED,
                actor="validator",
                reason=reason,
                evidence=TransitionEvidence(
                    kind=TransitionEvidenceKind.VALIDATION,
                    reference=validation.id,
                ),
                trusted=True,
            )
        await self._finish_execution(
            execution, ExecutionStatus.BLOCKED, reason, "VALIDATION_SKIPPED"
        )
        return True

    async def _finish_blocked_validation(self, task, execution, validation) -> None:
        reason = validation.summary
        await self._event(
            task.id,
            execution.id,
            "validation.system_blocked",
            reason,
            {"commands": _validation_evidence(validation)},
        )
        current = await self.tasks.get(task.id)
        if current and current.status == TaskStatus.VALIDATING:
            await WorkflowService(self.tasks).transition(
                current,
                TaskStatus.BLOCKED,
                actor="validator",
                reason=reason,
                evidence=TransitionEvidence(
                    kind=TransitionEvidenceKind.VALIDATION,
                    reference=validation.id,
                ),
                trusted=True,
            )
        await self._finish_execution(
            execution, ExecutionStatus.BLOCKED, reason, "VALIDATION_BLOCKED"
        )

    async def _finish_repair_stalled_validation(
        self, task, execution, validation, *, reason: str, repair_rounds: int
    ) -> None:
        await self._event(
            task.id,
            execution.id,
            "validation.repair_stalled",
            reason,
            {
                "repair_rounds": repair_rounds,
                "commands": _validation_evidence(validation),
            },
        )
        current = await self.tasks.get(task.id)
        if current and current.status == TaskStatus.VALIDATING:
            await WorkflowService(self.tasks).transition(
                current,
                TaskStatus.BLOCKED,
                actor="validator",
                reason=reason,
                evidence=TransitionEvidence(
                    kind=TransitionEvidenceKind.VALIDATION,
                    reference=validation.id,
                ),
                trusted=True,
            )
        await self._finish_execution(
            execution, ExecutionStatus.BLOCKED, reason, "VALIDATION_REPAIR_STALLED"
        )

    async def stop_active_processes(self, execution_id: str) -> None:
        workspace_stop = self._workspace_stop_events.get(execution_id)
        if workspace_stop is not None:
            workspace_stop.set()
        cancel = getattr(self.validator, "cancel_execution", None)
        if cancel is not None:
            result = cancel(execution_id)
            if hasattr(result, "__await__"):
                await result

    async def _mark_task(self, task_id: str, status: TaskStatus, reason: str) -> None:
        current = await self.tasks.get(task_id)
        if current and status in {TaskStatus.BLOCKED, TaskStatus.FAILED}:
            try:
                await WorkflowService(self.tasks).transition(
                    current, status, actor="system", reason=reason, trusted=True
                )
            except Exception:
                pass

    async def _finish_execution(self, execution, status, message: str, final_result: str) -> None:
        now = datetime.now(timezone.utc)
        current = await self.records.executions.get(execution.id)
        if current:
            await self.records.executions.replace(
                current.model_copy(
                    update={
                        "status": status,
                        "updated_at": now,
                        "finished_at": now,
                        "error_details": message[:2000],
                        "final_result": final_result,
                    }
                ),
                expected_updated_at=current.updated_at,
            )

    async def _event(
        self, task_id: str, execution_id: str, kind: str, message: str, metadata=None
    ) -> None:
        await self.records.activity_events.create(
            ActivityEvent(
                id=__import__("uuid").uuid4().hex,
                task_id=task_id,
                execution_id=execution_id,
                worker_name="system",
                event_type=kind,
                message=message[:2000],
                occurred_at=datetime.now(timezone.utc),
                source=ActivitySource.WORKER,
                metadata=metadata or {},
            )
        )
