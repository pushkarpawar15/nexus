from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
import tempfile
from pathlib import Path

from app.core.config import Settings
from app.core.logging import redact_credentials
from app.integrations.validation_runner import compute_patch_fingerprint
from app.repositories.records import WorkflowRepositories
from app.repositories.tasks import TaskRepository
from app.schemas.execution import ExecutionStatus
from app.schemas.security import SecurityStatus
from app.schemas.validation import ValidationStatus

logger = logging.getLogger(__name__)


class PublicationGate:
    def __init__(
        self, settings: Settings, tasks: TaskRepository, records: WorkflowRepositories
    ) -> None:
        self.settings, self.tasks, self.records = settings, tasks, records

    async def check(
        self,
        task_id: str,
        execution_id: str,
        *,
        workspace_override: Path | None = None,
        workspace_root_override: Path | None = None,
    ) -> tuple[bool, str | None]:
        task = await self.tasks.get(task_id)
        execution = await self.records.executions.get(execution_id)
        if task is None or execution is None or execution.task_id != task_id:
            return False, "Task or execution was not found."
        if (
            execution.status != ExecutionStatus.SUCCEEDED
            or execution.final_result != "PATCH_VALIDATED"
        ):
            return False, "Execution has not completed successfully."
        if not execution.patch_fingerprint or not execution.workspace_reference:
            return False, "Execution has no verified patch workspace."
        if workspace_override is None and execution.workspace_reference.startswith("gs://"):
            with tempfile.TemporaryDirectory(prefix="nexus-publication-check-") as temporary:
                from app.integrations.workspace_artifact import download_workspace

                try:
                    workspace = await asyncio.to_thread(
                        download_workspace,
                        self.settings,
                        execution.workspace_reference,
                        Path(temporary) / "workspace",
                    )
                except Exception:
                    return False, "Execution workspace is unavailable."
                return await self.check(
                    task_id,
                    execution_id,
                    workspace_override=workspace,
                    workspace_root_override=Path(temporary),
                )
        root = workspace_root_override or self.settings.workspace_root
        workspace = workspace_override or Path(execution.workspace_reference)
        if root is None or workspace.is_symlink() or not workspace.exists():
            return False, "Execution workspace is unavailable."
        resolved_root, resolved_workspace = root.resolve(), workspace.resolve()
        if resolved_workspace == resolved_root or resolved_root not in resolved_workspace.parents:
            return False, "Execution workspace is outside the configured private root."
        target = self.settings.approved_repositories.get(task.repository_key)
        if target is None or execution.repository_key != task.repository_key:
            return False, "Execution repository is not approved."
        try:
            from app.services.coding_workspace import _remote_matches, _run_git

            origin = await __import__("asyncio").to_thread(
                _run_git, self.settings, ["remote", "get-url", "origin"], cwd=resolved_workspace
            )
            branch = await __import__("asyncio").to_thread(
                _run_git, self.settings, ["branch", "--show-current"], cwd=resolved_workspace
            )
            if not _remote_matches(origin, target) or branch != execution.branch_reference:
                return False, "Workspace origin or branch no longer matches the execution."
            pull = await self.records.pull_requests.get(execution_id)
            if pull and pull.commit_sha:
                clean = await asyncio.to_thread(
                    _git_output,
                    self.settings,
                    resolved_workspace,
                    ["status", "--porcelain", "--untracked-files=all"],
                )
                head = await asyncio.to_thread(
                    _git_output, self.settings, resolved_workspace, ["rev-parse", "HEAD"]
                )
                if clean or head != pull.commit_sha or not execution.base_commit_sha:
                    return False, "Committed workspace no longer matches the PR checkpoint."
                diff = await asyncio.to_thread(
                    _git_bytes,
                    self.settings,
                    resolved_workspace,
                    [
                        "diff",
                        "--binary",
                        "--full-index",
                        f"{execution.base_commit_sha}..HEAD",
                        "--",
                    ],
                )
                fingerprint = hashlib.sha256(diff).hexdigest()
            else:
                fingerprint = await asyncio.to_thread(compute_patch_fingerprint, resolved_workspace)
        except Exception as exc:
            logger.warning(
                "Publication gate could not verify the workspace patch for execution %s: %s",
                execution_id,
                " ".join(redact_credentials(str(exc)).split())[:500] or type(exc).__name__,
            )
            return False, "Current workspace patch could not be verified."
        if fingerprint != execution.patch_fingerprint:
            return False, "Workspace patch is stale."
        reports = await self.records.validation_reports.list_for_task(task_id)
        report = max(
            (r for r in reports if r.execution_id == execution_id),
            key=lambda r: (r.finished_at, r.id),
            default=None,
        )
        if (
            report is None
            or report.overall_status != ValidationStatus.PASSED
            or report.patch_fingerprint != fingerprint
        ):
            return False, "A matching passing validation report is required."
        reviews = await self.records.security_reviews.list_for_task(task_id)
        review = max(
            (r for r in reviews if r.execution_id == execution_id),
            key=lambda r: (r.finished_at, r.id),
            default=None,
        )
        security_required = bool(
            task.execution_plan and task.execution_plan.security_review_required
        )
        if (security_required or review is not None) and (
            review is None
            or review.overall_status != SecurityStatus.PASSED
            or review.patch_fingerprint != fingerprint
        ):
            return False, "A matching passing security review is required."
        return True, None


def _git_output(settings, cwd: Path, args: list[str]) -> str:
    from app.services.coding_workspace import _git

    return _git(settings, args, cwd=cwd)


def _git_bytes(settings, cwd: Path, args: list[str]) -> bytes:
    from app.services.coding_workspace import _git_env

    result = subprocess.run(
        ["git", "-c", "core.pager=cat", *args],
        cwd=cwd,
        env=_git_env(settings),
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise ValueError("could not inspect committed patch")
    return result.stdout
