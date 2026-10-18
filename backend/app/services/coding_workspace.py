from __future__ import annotations

import base64
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.config import RepositoryTarget, Settings
from app.core.logging import redact_credentials


class CodingWorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodingWorkspace:
    path: Path
    branch: str
    base_commit_sha: str
    remote_url: str


def canonical_remote(target: RepositoryTarget) -> str:
    return f"https://github.com/{target.owner}/{target.repo}.git"


def _git_env(settings: Settings, *, with_token: bool = False) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/tmp/nexus-git-home",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_CONFIG_COUNT": "1" if with_token and settings.github_token else "0",
    }
    if with_token and settings.github_token:
        raw = f"x-access-token:{settings.github_token.get_secret_value()}".encode()
        env.update(
            {
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {base64.b64encode(raw).decode()}",
            }
        )
    return env


def _run_git(
    settings: Settings,
    args: list[str],
    *,
    cwd: Path | None = None,
    token: bool = False,
    stop_event: threading.Event | None = None,
) -> str:
    command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", *args]
    try:
        env = _git_env(settings, with_token=token)
        if token and settings.github_token:
            # Keep the token out of the URL, argv, and persistent config. Git's credential
            # helper is noninteractive and is used if GitHub challenges the extraheader.
            with tempfile.TemporaryDirectory(prefix="nexus-git-credential-") as helper_dir:
                helper = Path(helper_dir) / "credential-helper"
                helper.write_text(
                    "#!/bin/sh\n"
                    'case "$1" in\n'
                    "  get)\n"
                    "    printf 'username=%s\\n' \"$NEXUS_GIT_USERNAME\"\n"
                    "    printf 'password=%s\\n' \"$NEXUS_GIT_PASSWORD\"\n"
                    "    printf '\\n'\n"
                    "    ;;\n"
                    "esac\n",
                    encoding="utf-8",
                )
                helper.chmod(0o700)
                env.update(
                    {
                        "NEXUS_GIT_USERNAME": "x-access-token",
                        "NEXUS_GIT_PASSWORD": settings.github_token.get_secret_value(),
                    }
                )
                authenticated_command = [
                    "git",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "credential.helper=",
                    "-c",
                    f"credential.helper={helper}",
                    *args,
                ]
                completed = _run_git_process(
                    authenticated_command,
                    cwd=cwd,
                    env=env,
                    stop_event=stop_event,
                )
        else:
            completed = _run_git_process(
                command,
                cwd=cwd,
                env=env,
                stop_event=stop_event,
            )
    except InterruptedError as exc:
        operation = _git_operation(args)
        raise CodingWorkspaceError(f"Git {operation} stopped by operator.") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        operation = _git_operation(args)
        raise CodingWorkspaceError(f"Git {operation} timed out or is unavailable.") from exc
    if completed.returncode:
        operation = _git_operation(args)
        detail = redact_credentials(completed.stderr.strip())
        detail = " ".join(detail.split())[:900]
        message = f"Git {operation} failed"
        if detail:
            message += f": {detail}"
        raise CodingWorkspaceError(message)
    # Preserve leading spaces: porcelain status uses them as part of its two-column state.
    # Trimming them causes callers to slice the first path character off the first entry.
    return completed.stdout.rstrip("\r\n")


def _run_git_process(
    command: list[str],
    *,
    cwd: Path | None,
    env: dict[str, str],
    stop_event: threading.Event | None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 90
    while True:
        if stop_event is not None and stop_event.is_set():
            if process.poll() is not None:
                raise InterruptedError("Git operation stopped")
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate()
            raise InterruptedError("Git operation stopped")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise subprocess.TimeoutExpired(command, 90)
        try:
            stdout, stderr = process.communicate(timeout=min(0.2, remaining))
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            continue


def _git_operation(args: list[str]) -> str:
    if not args:
        return "operation"
    if args[0] in {"remote", "checkout", "rev-parse"} and len(args) > 1:
        return f"{args[0]} {args[1]}"
    return args[0]


def _remote_matches(actual: str, target: RepositoryTarget) -> bool:
    normalized = actual.strip().removesuffix("/").removesuffix(".git").lower()
    expected = canonical_remote(target).removesuffix(".git").lower()
    return normalized in {expected, f"git@github.com:{target.owner}/{target.repo}".lower()}


def create_workspace(
    settings: Settings,
    target: RepositoryTarget,
    task_id: str,
    execution_id: str,
    *,
    branch_name: str | None = None,
    stop_event: threading.Event | None = None,
) -> CodingWorkspace:
    if settings.workspace_root is None:
        raise CodingWorkspaceError("Private workspace root is not configured.")
    root = settings.workspace_root
    if root.is_symlink():
        raise CodingWorkspaceError("Workspace root must not be a symlink.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = root.resolve(strict=True)
    os.chmod(root, 0o700)
    nexus_root = Path(__file__).resolve().parents[3]
    source = target.source_path
    if source is not None:
        if source.is_symlink() or not source.is_dir():
            raise CodingWorkspaceError("Configured source checkout is missing or unsafe.")
        source = source.resolve(strict=True)
        if source == nexus_root or nexus_root in source.parents or source in nexus_root.parents:
            raise CodingWorkspaceError("Target checkout overlaps the NEXUS source tree.")
        try:
            origin = _run_git(
                settings, ["remote", "get-url", "origin"], cwd=source, stop_event=stop_event
            )
        except CodingWorkspaceError as exc:
            raise CodingWorkspaceError(
                "Configured target checkout has no verified Git origin."
            ) from exc
        if not _remote_matches(origin, target):
            raise CodingWorkspaceError(
                "Configured target checkout origin does not match the allowlist."
            )
    if source is not None and (source == root or source in root.parents or root in source.parents):
        raise CodingWorkspaceError("Workspace root must be separate from the target checkout.")
    if source is None and (
        settings.github_token is None or not settings.github_token.get_secret_value().strip()
    ):
        raise CodingWorkspaceError(
            "No active GitHub credential is available for this remote repository. Reconnect "
            "the PAT in the Repositories page or restore GitHub App access, then retry."
        )
    safe_task = re.sub(r"[^a-zA-Z0-9-]", "-", task_id)[:24]
    safe_execution = re.sub(r"[^a-zA-Z0-9-]", "-", execution_id)[:24]
    branch = branch_name or f"nexus/task-{safe_task}-{safe_execution}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,249}", branch):
        raise CodingWorkspaceError("Requested follow-up branch name is unsafe.")
    destination = root / f"execution-{safe_execution}"
    if destination.exists() or destination.is_symlink():
        raise CodingWorkspaceError("Execution workspace already exists.")
    destination.mkdir(mode=0o700)
    remote = canonical_remote(target)
    try:
        source_ref = str(source) if source else remote
        _run_git(
            settings,
            [
                "clone",
                "--no-local",
                "--single-branch",
                "--branch",
                target.base_branch,
                source_ref,
                str(destination),
            ],
            token=source is None,
            stop_event=stop_event,
        )
        _run_git(
            settings,
            ["remote", "set-url", "origin", remote],
            cwd=destination,
            stop_event=stop_event,
        )
        actual_origin = _run_git(
            settings, ["remote", "get-url", "origin"], cwd=destination, stop_event=stop_event
        )
        if actual_origin != remote:
            raise CodingWorkspaceError("Cloned checkout origin verification failed.")
        base_sha = _run_git(settings, ["rev-parse", "HEAD"], cwd=destination, stop_event=stop_event)
        if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
            raise CodingWorkspaceError("Base commit could not be verified.")
        _run_git(settings, ["checkout", "-b", branch], cwd=destination, stop_event=stop_event)
        return CodingWorkspace(destination, branch, base_sha, remote)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def cleanup_workspace(settings: Settings, workspace: CodingWorkspace) -> None:
    """Remove only this execution's direct child of the configured workspace root."""
    if settings.workspace_root is None:
        return
    root = settings.workspace_root.resolve(strict=True)
    candidate = workspace.path
    if candidate.parent.resolve(strict=True) != root:
        raise CodingWorkspaceError("Refusing to remove a workspace outside the configured root.")
    if candidate.is_symlink():
        candidate.unlink()
    elif candidate.exists():
        shutil.rmtree(candidate)


__all__ = [
    "CodingWorkspace",
    "CodingWorkspaceError",
    "canonical_remote",
    "cleanup_workspace",
    "create_workspace",
]
