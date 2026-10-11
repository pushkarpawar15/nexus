from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

from app.core.config import Settings
from app.core.logging import redact_credentials
from app.schemas.execution import ExecutionRecord

_MAX_DIFF_BYTES = 512 * 1024
_TOKEN = re.compile(
    r"(?i)(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"AIza[0-9A-Za-z_-]{30,}|AKIA[0-9A-Z]{16})"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.DOTALL,
)


class ExecutionDiffError(RuntimeError):
    pass


def read_execution_diff(
    settings: Settings,
    execution: ExecutionRecord,
    pull_request=None,
) -> dict[str, object]:
    if not execution.patch_fingerprint or not execution.workspace_reference:
        raise ExecutionDiffError("No code patch is available for this execution.")
    if not execution.branch_reference or not execution.base_commit_sha:
        raise ExecutionDiffError("Workspace branch evidence is incomplete.")
    root = settings.workspace_root
    if root is None or root.is_symlink():
        raise ExecutionDiffError("Private execution workspace is unavailable.")
    workspace = Path(execution.workspace_reference)
    if workspace.is_symlink() or not workspace.is_dir():
        raise ExecutionDiffError("Execution workspace is unavailable.")
    resolved_root = root.resolve()
    resolved_workspace = workspace.resolve()
    if resolved_workspace == resolved_root or resolved_root not in resolved_workspace.parents:
        raise ExecutionDiffError("Execution workspace is outside the configured private root.")
    target = settings.approved_repositories.get(execution.repository_key or "")
    if target is None:
        raise ExecutionDiffError("Execution repository is no longer approved.")

    from app.services.coding_workspace import _remote_matches, _run_git

    try:
        origin = _run_git(settings, ["remote", "get-url", "origin"], cwd=resolved_workspace)
        branch = _run_git(settings, ["branch", "--show-current"], cwd=resolved_workspace)
        head = _run_git(settings, ["rev-parse", "HEAD"], cwd=resolved_workspace)
        if not _remote_matches(origin, target) or branch != execution.branch_reference:
            raise ExecutionDiffError("Workspace repository or branch does not match the execution.")
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", execution.base_commit_sha):
            raise ExecutionDiffError("Base commit SHA is invalid.")

        committed = bool(pull_request and pull_request.commit_sha)
        if committed:
            status = _run_git(
                settings,
                ["status", "--porcelain", "--untracked-files=all"],
                cwd=resolved_workspace,
            )
            if status.strip() or head != pull_request.commit_sha:
                raise ExecutionDiffError("Committed workspace no longer matches its saved checkpoint.")
            args = [
                "diff",
                "--binary",
                "--full-index",
                f"{execution.base_commit_sha}..HEAD",
                "--",
            ]
        else:
            if head != execution.base_commit_sha:
                raise ExecutionDiffError("Workspace HEAD no longer matches its saved base commit.")
            args = ["diff", "--binary", "--full-index", "HEAD", "--"]
        result = subprocess.run(
            ["git", "-c", "core.pager=cat", *args],
            cwd=resolved_workspace,
            env=__import__("app.services.coding_workspace", fromlist=["_git_env"])._git_env(
                settings
            ),
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode or len(result.stdout) > _MAX_DIFF_BYTES:
            raise ExecutionDiffError("Patch could not be read within the preview limit.")
        fingerprint = hashlib.sha256(result.stdout).hexdigest()
        if fingerprint != execution.patch_fingerprint:
            raise ExecutionDiffError("Workspace patch changed after execution evidence was saved.")
    except ExecutionDiffError:
        raise
    except Exception as exc:
        detail = " ".join(redact_credentials(str(exc)).split())[:500]
        raise ExecutionDiffError(
            detail or "Current workspace patch could not be verified."
        ) from exc

    diff = result.stdout.decode("utf-8", errors="replace")
    diff = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", diff)
    diff = _TOKEN.sub("[REDACTED TOKEN]", redact_credentials(diff))
    return {
        "execution_id": execution.id,
        "branch": execution.branch_reference,
        "base_commit_sha": execution.base_commit_sha,
        "commit_sha": pull_request.commit_sha if committed else None,
        "committed": committed,
        "patch_fingerprint": execution.patch_fingerprint,
        "changed_files": execution.changed_files,
        "diff": diff,
        "redacted": True,
    }
