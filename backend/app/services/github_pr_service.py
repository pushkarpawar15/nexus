from __future__ import annotations

import asyncio
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import SecretStr

from app.core.errors import ErrorCode, NexusError
from app.core.logging import redact_credentials
from app.integrations.github import GitHubClient, GitHubOperationError
from app.integrations.github_app import GitHubAppIntegration
from app.integrations.workspace_artifact import download_workspace
from app.integrations.validation_runner import compute_patch_fingerprint
from app.models.task import TaskStatus, TransitionEvidence, TransitionEvidenceKind
from app.repositories.records import WorkflowRepositories
from app.repositories.tasks import TaskRepository
from app.schemas.approval import ApprovalStatus
from app.schemas.execution import ActivityEvent, ActivitySource
from app.schemas.pull_request import PullRequestRecord, PullRequestStatus
from app.services.coding_workspace import _run_git
from app.services.publication_gates import PublicationGate
from app.services.workflow_service import WorkflowService


class GitHubPRService:
    def __init__(
        self,
        settings,
        tasks: TaskRepository,
        records: WorkflowRepositories,
        github=None,
        github_app: GitHubAppIntegration | None = None,
    ) -> None:
        self.settings, self.tasks, self.records = settings, tasks, records
        self.gate = PublicationGate(settings, tasks, records)
        self.github = github
        self.github_app = github_app
        self._lock = asyncio.Lock()

    async def eligibility(self, task_id: str, execution_id: str) -> tuple[bool, str | None]:
        task = await self.tasks.get(task_id)
        if task is None:
            return False, "Task was not found."
        if task.status != TaskStatus.APPROVED:
            return False, "Task must be approved before publication."
        target = self.settings.approved_repositories.get(task.repository_key)
        if not self.settings.github_token and not (
            target
            and target.github_installation_id
            and self.github_app
            and self.github_app.configured
        ):
            return False, "GitHub token is not configured."
        allowed, reason = await self.gate.check(task_id, execution_id)
        if not allowed:
            return False, reason
        execution = await self.records.executions.get(execution_id)
        if execution is None:
            return False, "Execution was not found."
        approvals = await self.records.approvals.list_for_task(task_id)
        for approval in approvals:
            if (
                approval.execution_id == execution_id
                and approval.patch_fingerprint == execution.patch_fingerprint
                and approval.status == ApprovalStatus.REJECTED
            ):
                return False, "This patch has a recorded human rejection."
        if task.approval_required and not any(
            a.execution_id == execution_id
            and a.patch_fingerprint == execution.patch_fingerprint
            and a.status == ApprovalStatus.APPROVED
            for a in approvals
        ):
            return False, "A matching administrator approval is required."
        return True, None

    async def create(self, task_id: str, execution_id: str) -> PullRequestRecord:
        async with self._lock:
            execution = await self.records.executions.get(execution_id)
            if execution is None or not (execution.workspace_reference or "").startswith("gs://"):
                return await self._create_locked(task_id, execution_id)
            with tempfile.TemporaryDirectory(prefix="nexus-publish-") as temporary:
                workspace = await asyncio.to_thread(
                    download_workspace,
                    self.settings,
                    execution.workspace_reference,
                    Path(temporary) / "workspace",
                )
                return await self._create_locked(
                    task_id, execution_id, workspace_override=workspace
                )

    async def _create_locked(
        self, task_id: str, execution_id: str, *, workspace_override: Path | None = None
    ) -> PullRequestRecord:
        task = await self.tasks.get(task_id)
        execution = await self.records.executions.get(execution_id)
        if task is None:
            raise NexusError(ErrorCode.TASK_NOT_FOUND, "Task was not found.", status_code=404)
        if execution is None or execution.task_id != task_id:
            raise NexusError(
                ErrorCode.EXECUTION_NOT_FOUND, "Execution was not found.", status_code=404
            )
        existing = await self.records.pull_requests.get(execution_id)
        if existing and existing.status == PullRequestStatus.CREATED:
            await self._finish_transition(task, existing)
            return existing
        if (
            existing
            and existing.error_details is None
            and (datetime.now(timezone.utc) - existing.updated_at).total_seconds() < 300
        ):
            raise NexusError(
                ErrorCode.PULL_REQUEST_CONFLICT,
                "A publication request is already in progress for this execution.",
                status_code=409,
            )
        eligible, reason = await self.eligibility(task_id, execution_id)
        if not eligible:
            raise NexusError(
                ErrorCode.PUBLICATION_BLOCKED,
                reason or "Publication is blocked because a required gate did not pass.",
                status_code=409,
            )
        target = self.settings.approved_repositories[task.repository_key]
        workspace = workspace_override or Path(execution.workspace_reference or "")
        if (
            not workspace.is_dir()
            or not execution.branch_reference
            or not execution.patch_fingerprint
        ):
            raise NexusError(
                ErrorCode.PUBLICATION_BLOCKED,
                "Execution workspace or patch evidence is missing.",
                status_code=409,
            )
        now = datetime.now(timezone.utc)
        record = existing or PullRequestRecord(
            id=execution_id,
            task_id=task_id,
            execution_id=execution_id,
            patch_fingerprint=execution.patch_fingerprint,
            repository_key=task.repository_key,
            base_branch=target.base_branch,
            branch_name=execution.branch_reference,
            status=PullRequestStatus.PREPARING,
            created_at=now,
            updated_at=now,
        )
        if existing is None:
            record = await self.records.pull_requests.create(record)
        await self._event(
            task_id,
            execution_id,
            "github.started" if existing is None else "github.resumed",
            "GitHub publication checkpoint started.",
        )
        client = self.github
        owned_client = client is None
        try:
            operation_settings = self.settings
            if target.github_installation_id and self.github_app is not None:
                token = await self.github_app.installation_token(target.github_installation_id)
                operation_settings = self.settings.model_copy(
                    update={"github_token": SecretStr(token)}
                )
            if client is None:
                client = GitHubClient(operation_settings)
            await client.verify_target(target)
            if record.status == PullRequestStatus.PREPARING:
                current_fp = await asyncio.to_thread(compute_patch_fingerprint, workspace)
                if current_fp != record.patch_fingerprint:
                    raise GitHubOperationError("Patch changed before commit.")
                status = await asyncio.to_thread(
                    _run_git,
                    operation_settings,
                    ["status", "--porcelain", "--untracked-files=all"],
                    cwd=workspace,
                )
                changed = sorted(line[3:] for line in status.splitlines() if line.strip())
                if not changed or changed != sorted(execution.changed_files):
                    await self._event(
                        task_id,
                        execution_id,
                        "github.files_mismatch",
                        "Workspace file set does not match the files recorded at validation.",
                        {
                            "workspace_files": changed,
                            "validated_files": sorted(execution.changed_files),
                        },
                    )
                    raise GitHubOperationError(
                        "Workspace changes differ from the validated file list: "
                        f"workspace={changed}; validated={sorted(execution.changed_files)}."
                    )
                await asyncio.to_thread(_git_commit, operation_settings, workspace, task.title)
                commit_sha = await asyncio.to_thread(
                    _run_git, operation_settings, ["rev-parse", "HEAD"], cwd=workspace
                )
                if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
                    raise GitHubOperationError("Git did not confirm the commit SHA.")
                updated_record = record.model_copy(
                    update={
                        "status": PullRequestStatus.COMMITTED,
                        "commit_sha": commit_sha,
                        "updated_at": datetime.now(timezone.utc),
                        "error_details": None,
                    }
                )
                await self.records.pull_requests.replace(
                    updated_record, expected_updated_at=record.updated_at
                )
                record = updated_record
                await self._event(
                    task_id,
                    execution_id,
                    "github.committed",
                    f"Validated patch committed on {record.branch_name} at {commit_sha[:12]}.",
                    {"branch": record.branch_name, "commit_sha": commit_sha},
                )
            if record.status == PullRequestStatus.COMMITTED:
                head = await asyncio.to_thread(
                    _run_git, operation_settings, ["rev-parse", "HEAD"], cwd=workspace
                )
                if head != record.commit_sha:
                    raise GitHubOperationError(
                        "Execution branch no longer matches the saved commit."
                    )
                await asyncio.to_thread(
                    _run_git,
                    operation_settings,
                    ["push", "origin", f"HEAD:refs/heads/{record.branch_name}"],
                    cwd=workspace,
                    token=True,
                )
                updated_record = record.model_copy(
                    update={
                        "status": PullRequestStatus.PUSHED,
                        "updated_at": datetime.now(timezone.utc),
                        "error_details": None,
                    }
                )
                await self.records.pull_requests.replace(
                    updated_record, expected_updated_at=record.updated_at
                )
                record = updated_record
                await self._event(
                    task_id,
                    execution_id,
                    "github.pushed",
                    f"Execution branch {record.branch_name} was pushed to GitHub.",
                    {"branch": record.branch_name, "commit_sha": record.commit_sha},
                )
            if record.status == PullRequestStatus.PUSHED:
                pr = await client.find_pull_request(target, record.branch_name)
                if pr is None:
                    try:
                        pr = await client.create_pull_request(
                            target,
                            task.title,
                            record.branch_name,
                            body=(
                                "## NEXUS task\n\n"
                                f"{task.description[:6000]}\n\n"
                                "## Validation\n\n"
                                f"{execution.test_summary or 'Configured validation passed.'}\n\n"
                                "## Security review\n\n"
                                f"{execution.security_summary or 'Not required.'}\n\n"
                                "Generated from a task-specific, validated NEXUS patch."
                            ),
                        )
                    except GitHubOperationError:
                        pr = await client.find_pull_request(target, record.branch_name)
                        if pr is None:
                            raise
                expected_url = (
                    f"https://github.com/{target.owner}/{target.repo}/pull/{pr.get('number')}"
                )
                if (
                    pr.get("state") != "open"
                    or pr.get("head", {}).get("sha") != record.commit_sha
                    or pr.get("html_url") != expected_url
                    or not pr.get("number")
                ):
                    raise GitHubOperationError(
                        "GitHub returned a pull request that did not match the pushed commit."
                    )
                updated_record = record.model_copy(
                    update={
                        "status": PullRequestStatus.CREATED,
                        "pr_number": int(pr["number"]),
                        "pr_url": expected_url,
                        "updated_at": datetime.now(timezone.utc),
                        "error_details": None,
                    }
                )
                await self.records.pull_requests.replace(
                    updated_record, expected_updated_at=record.updated_at
                )
                record = updated_record
                await self._event(
                    task_id,
                    execution_id,
                    "github.created",
                    f"GitHub confirmed pull request #{record.pr_number}.",
                    {
                        "branch": record.branch_name,
                        "commit_sha": record.commit_sha,
                        "pr_number": record.pr_number,
                        "pr_url": record.pr_url,
                    },
                )
            current_task = await self.tasks.get(task_id)
            if current_task:
                await self._finish_transition(current_task, record)
            return record
        except Exception as exc:
            current = await self.records.pull_requests.get(execution_id)
            if current:
                detail = " ".join(redact_credentials(str(exc)).split())
                message = (
                    f"{type(exc).__name__}: {detail}"[:1000]
                    if detail
                    else f"{type(exc).__name__}: GitHub publication failed."
                )
                await self.records.pull_requests.replace(
                    current.model_copy(
                        update={"updated_at": datetime.now(timezone.utc), "error_details": message}
                    ),
                    expected_updated_at=current.updated_at,
                )
                await self._event(task_id, execution_id, "github.failed", message)
            raise NexusError(
                ErrorCode.GITHUB_OPERATION_ERROR,
                "GitHub could not confirm PR creation. Retry to resume the saved checkpoint.",
                status_code=502,
            ) from exc
        finally:
            if owned_client and client is not None:
                await client.close()

    async def handle_pull_request_webhook(self, payload: dict) -> None:
        """Complete a task when GitHub reports that its recorded PR was merged."""
        if payload.get("action") != "closed":
            return
        pull_request = payload.get("pull_request")
        repository = payload.get("repository")
        if not isinstance(pull_request, dict) or pull_request.get("merged") is not True:
            return
        if not isinstance(repository, dict):
            return
        full_name = repository.get("full_name")
        number = pull_request.get("number")
        head = pull_request.get("head")
        head_ref = head.get("ref") if isinstance(head, dict) else None
        merge_sha = pull_request.get("merge_commit_sha")
        merged_at = pull_request.get("merged_at")
        if (
            not isinstance(full_name, str)
            or not isinstance(number, int)
            or not isinstance(head_ref, str)
            or not isinstance(merge_sha, str)
            or not re.fullmatch(r"[0-9a-fA-F]{7,64}", merge_sha)
            or not isinstance(merged_at, str)
        ):
            return

        matches = []
        for record in await self.records.pull_requests.list_all():
            if record.status not in {PullRequestStatus.CREATED, PullRequestStatus.MERGED}:
                continue
            task = await self.tasks.get(record.task_id)
            target = self.settings.approved_repositories.get(record.repository_key)
            if (
                task is not None
                and target is not None
                and f"{target.owner}/{target.repo}".casefold() == full_name.casefold()
                and record.pr_number == number
                and record.branch_name == head_ref
                and record.pr_url
                == f"https://github.com/{target.owner}/{target.repo}/pull/{number}"
            ):
                matches.append((record, task))
        if len(matches) != 1:
            return

        record, task = matches[0]
        try:
            merged_time = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
        except ValueError:
            return
        if record.status != PullRequestStatus.MERGED:
            updated = record.model_copy(
                update={
                    "status": PullRequestStatus.MERGED,
                    "merge_commit_sha": merge_sha,
                    "merged_at": merged_time,
                    "updated_at": datetime.now(timezone.utc),
                    "error_details": None,
                }
            )
            await self.records.pull_requests.replace(updated, expected_updated_at=record.updated_at)
            record = updated
            await self._event(
                record.task_id,
                record.execution_id,
                "github.merged",
                f"GitHub confirmed pull request #{record.pr_number} merged.",
                {
                    "pr_number": record.pr_number,
                    "pr_url": record.pr_url,
                    "merge_commit_sha": merge_sha,
                    "merged_at": merged_time.isoformat(),
                    "merged_by": (
                        pull_request.get("merged_by", {}).get("login")
                        if isinstance(pull_request.get("merged_by"), dict)
                        else None
                    ),
                },
            )
        task = await self.tasks.get(record.task_id)
        if task is not None and task.status == TaskStatus.PR_CREATED:
            reference = f"{record.pr_url}@{merge_sha}"
            await WorkflowService(self.tasks).transition(
                task,
                TaskStatus.COMPLETED,
                actor="github_webhook",
                reason=f"GitHub confirmed pull request #{record.pr_number} was merged.",
                evidence=TransitionEvidence(
                    kind=TransitionEvidenceKind.COMPLETION, reference=reference
                ),
                trusted=True,
            )

    async def sync_pull_request(self, task_id: str, execution_id: str) -> PullRequestRecord:
        task = await self.tasks.get(task_id)
        record = await self.records.pull_requests.get(execution_id)
        if task is None or record is None or record.task_id != task_id:
            raise NexusError(
                ErrorCode.EXECUTION_NOT_FOUND, "Pull request was not found.", status_code=404
            )
        if record.status == PullRequestStatus.MERGED:
            if task.status == TaskStatus.PR_CREATED:
                await self._complete_task_for_merge(record, task)
            return record
        if record.status != PullRequestStatus.CREATED or record.pr_number is None:
            raise NexusError(
                ErrorCode.PUBLICATION_BLOCKED,
                "A confirmed pull request is required.",
                status_code=409,
            )

        target = self.settings.approved_repositories.get(record.repository_key)
        if target is None:
            raise NexusError(
                ErrorCode.PUBLICATION_BLOCKED,
                "The pull request repository is not configured.",
                status_code=409,
            )
        operation_settings = self.settings
        if target.github_installation_id and self.github_app is not None:
            token = await self.github_app.installation_token(target.github_installation_id)
            operation_settings = self.settings.model_copy(update={"github_token": SecretStr(token)})
        client = self.github
        owned_client = client is None
        try:
            if client is None:
                client = GitHubClient(operation_settings)
            pr = await client.get_pull_request(target, record.pr_number)
            if (
                pr.get("number") != record.pr_number
                or pr.get("html_url") != record.pr_url
                or pr.get("head", {}).get("ref") != record.branch_name
            ):
                raise GitHubOperationError("GitHub returned a different pull request.")
            if pr.get("merged") is not True:
                return record
            merge_sha = pr.get("merge_commit_sha")
            merged_at = pr.get("merged_at")
            if not isinstance(merge_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", merge_sha):
                raise GitHubOperationError("GitHub did not confirm the merge commit SHA.")
            if not isinstance(merged_at, str):
                raise GitHubOperationError(
                    "GitHub did not confirm when the pull request was merged."
                )
            merged_time = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
            updated = record.model_copy(
                update={
                    "status": PullRequestStatus.MERGED,
                    "merge_commit_sha": merge_sha,
                    "merged_at": merged_time,
                    "updated_at": datetime.now(timezone.utc),
                    "error_details": None,
                }
            )
            await self.records.pull_requests.replace(updated, expected_updated_at=record.updated_at)
            record = updated
            await self._event(
                task_id,
                execution_id,
                "github.merged",
                f"GitHub confirmed pull request #{record.pr_number} merged.",
                {
                    "pr_number": record.pr_number,
                    "pr_url": record.pr_url,
                    "merge_commit_sha": merge_sha,
                    "merged_at": merged_time.isoformat(),
                },
            )
            task = await self.tasks.get(task_id)
            if task is not None:
                await self._complete_task_for_merge(record, task)
            return record
        finally:
            if owned_client and client is not None:
                await client.close()

    async def _complete_task_for_merge(self, record, task) -> None:
        if task.status == TaskStatus.PR_CREATED and record.merge_commit_sha:
            await WorkflowService(self.tasks).transition(
                task,
                TaskStatus.COMPLETED,
                actor="github_sync",
                reason=f"GitHub confirmed pull request #{record.pr_number} was merged.",
                evidence=TransitionEvidence(
                    kind=TransitionEvidenceKind.COMPLETION,
                    reference=f"{record.pr_url}@{record.merge_commit_sha}",
                ),
                trusted=True,
            )

    async def _finish_transition(self, task, record):
        if task.status == TaskStatus.APPROVED:
            await WorkflowService(self.tasks).transition(
                task,
                TaskStatus.PR_CREATED,
                actor="github",
                reason="GitHub confirmed the pull request.",
                evidence=TransitionEvidence(
                    kind=TransitionEvidenceKind.GITHUB_PR, reference=record.pr_url or ""
                ),
                trusted=True,
            )

    async def _event(self, task_id, execution_id, kind, message, metadata=None):
        await self.records.activity_events.create(
            ActivityEvent(
                id=uuid4().hex,
                task_id=task_id,
                execution_id=execution_id,
                worker_name="github",
                event_type=kind,
                message=message,
                occurred_at=datetime.now(timezone.utc),
                source=ActivitySource.SYSTEM,
                metadata=metadata or {},
            )
        )


def _git_commit(settings, workspace, title):
    safe_title = " ".join(title.split())[:180]
    subprocess.run(
        ["git", "add", "-A"],
        cwd=workspace,
        env=__import__("app.services.coding_workspace", fromlist=["_git_env"])._git_env(settings),
        capture_output=True,
        timeout=30,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.name=NEXUS Bot",
            "-c",
            "user.email=nexus-bot@users.noreply.github.com",
            "commit",
            "-m",
            f"NEXUS: {safe_title}",
        ],
        cwd=workspace,
        env=__import__("app.services.coding_workspace", fromlist=["_git_env"])._git_env(settings),
        capture_output=True,
        timeout=60,
        check=True,
    )
