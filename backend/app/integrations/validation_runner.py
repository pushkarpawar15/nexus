from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from app.core.config import Settings
from app.core.logging import redact_credentials
from app.models.task import Task
from app.schemas.execution import ExecutionRecord
from app.schemas.validation import (
    ValidationCommandResult,
    ValidationCommandStatus,
    ValidationReport,
    ValidationStatus,
)

MAX_SOURCE_FILES = 5000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 30 * 1024 * 1024
MAX_CAPTURE_BYTES = 10_000
_TOKEN_PATTERN = re.compile(
    r"(?i)(?:gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|AKIA[0-9A-Z]{16})"
)
_CREDENTIAL_PATH = re.compile(
    r"(?i)(?:^|/)(?:credentials?\.json|id_rsa|id_ed25519|secrets?[^/]*|.*\.pem|.*\.p12|.*\.pfx)$"
)
DEFAULT_COMMAND_TIMEOUT_SECONDS = 120
_ACTIVE_CONTAINERS: dict[str, set[str]] = {}
_ACTIVE_VALIDATIONS: set[str] = set()
_CANCELLED_EXECUTIONS: set[str] = set()
_CONTAINER_LOCK = threading.Lock()
SKIP_DIRECTORIES = {
    ".git",
    ".venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "__pycache__",
    ".pytest_cache",
}
SKIP_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".sqlite", ".db"}


@dataclass(frozen=True)
class ValidationCommand:
    command_id: str
    cwd: str
    argv: tuple[str, ...]
    image: str


def payflow_commands(settings: Settings) -> tuple[ValidationCommand, ...]:
    return (
        ValidationCommand(
            "backend_pytest",
            "/workspace/backend",
            ("python", "-m", "pytest"),
            settings.validation_python_image,
        ),
        ValidationCommand(
            "backend_ruff",
            "/workspace/backend",
            ("ruff", "check", "."),
            settings.validation_python_image,
        ),
        ValidationCommand(
            "backend_format",
            "/workspace/backend",
            ("ruff", "format", "--check", "."),
            settings.validation_python_image,
        ),
        ValidationCommand(
            "frontend_test",
            "/workspace/frontend",
            ("npm", "test", "--", "--run"),
            settings.validation_node_image,
        ),
        ValidationCommand(
            "frontend_lint",
            "/workspace/frontend",
            ("npm", "run", "lint"),
            settings.validation_node_image,
        ),
        ValidationCommand(
            "frontend_typecheck",
            "/workspace/frontend",
            ("npm", "run", "typecheck"),
            settings.validation_node_image,
        ),
        ValidationCommand(
            "frontend_build",
            "/workspace/frontend",
            ("npm", "run", "build"),
            settings.validation_node_image,
        ),
    )


def compute_patch_fingerprint(source_root: Path) -> str:
    if (source_root / ".git").exists():
        result = subprocess.run(
            ["git", "-c", "core.pager=cat", "diff", "--binary", "--full-index", "HEAD", "--"],
            cwd=source_root,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": "/tmp/nexus-git-home",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
            },
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise ValueError("could not fingerprint the isolated Git diff")
        return hashlib.sha256(result.stdout).hexdigest()
    digest = hashlib.sha256()
    for relative_path, path in _source_files(source_root):
        digest.update(relative_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(64 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _source_files(source_root: Path) -> list[tuple[Path, Path]]:
    if source_root.is_symlink():
        raise ValueError("validation source root must not be a symlink")
    root = source_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("validation source must be a directory")
    files: list[tuple[Path, Path]] = []
    total_bytes = 0
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_root)
        kept_directories: list[str] = []
        for name in directory_names:
            path = current / name
            if name in SKIP_DIRECTORIES or name.startswith("."):
                continue
            if path.is_symlink():
                raise ValueError(f"source tree contains a symlink: {path.relative_to(root)}")
            kept_directories.append(name)
        directory_names[:] = kept_directories

        for name in file_names:
            path = current / name
            relative_name = path.relative_to(root).as_posix()
            if name.startswith(".env") or path.suffix.lower() in SKIP_SUFFIXES:
                continue
            if _CREDENTIAL_PATH.search(relative_name):
                raise ValueError("source tree contains a credential-like file path")
            if path.is_symlink() or not path.is_file():
                raise ValueError(
                    f"source tree contains a non-regular file: {path.relative_to(root)}"
                )
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise ValueError(f"source file exceeds the 2 MiB limit: {path.relative_to(root)}")
            try:
                path.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                continue
            total_bytes += size
            if total_bytes > MAX_TOTAL_SOURCE_BYTES:
                raise ValueError("validation source exceeds the 100 MiB total limit")
            if len(files) >= MAX_SOURCE_FILES:
                raise ValueError("validation source exceeds the 5,000 file limit")
            files.append((path.relative_to(root), path))
    files.sort(key=lambda item: item[0].as_posix())
    return files


def _copy_source(source_root: Path, destination: Path) -> None:
    files = _source_files(source_root)
    for relative_path, source in files:
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


def _capture_stream(stream: BinaryIO, captured: list[bytes]) -> None:
    kept = 0
    while chunk := stream.read(8192):
        if kept < MAX_CAPTURE_BYTES:
            portion = chunk[: MAX_CAPTURE_BYTES - kept]
            captured.append(portion)
            kept += len(portion)


def _run_container(
    command: ValidationCommand,
    staging: Path,
    *,
    timeout_seconds: int,
    execution_id: str,
) -> tuple[int | None, str, str, bool, str | None]:
    try:
        available_cpus = sorted(os.sched_getaffinity(0))[:2]
    except (AttributeError, OSError):  # pragma: no cover - non-Linux host
        available_cpus = []
    container_name = f"nexus-{execution_id[:24]}-{command.command_id}"
    docker_argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--network=none",
        "--read-only",
        "--security-opt=no-new-privileges",
        "--cap-drop=ALL",
        "--pids-limit=128",
        "--memory=2g",
        "--cpus=2",
        "--tmpfs=/tmp:rw,exec,nosuid,nodev,size=1g",
        "--mount",
        f"type=bind,src={staging},dst=/workspace,readonly",
        "--workdir",
        command.cwd,
        "--env",
        "HOME=/tmp",
        "--env",
        "TMPDIR=/tmp",
        "--env",
        "RUFF_CACHE_DIR=/tmp/ruff-cache",
        "--env",
        "PYTEST_ADDOPTS=-p no:cacheprovider",
        command.image,
        *command.argv,
    ]
    if available_cpus:
        docker_argv[docker_argv.index("--cpus=2") : docker_argv.index("--cpus=2")] = [
            "--cpuset-cpus=" + ",".join(str(cpu) for cpu in available_cpus)
        ]
    try:
        process = subprocess.Popen(
            docker_argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return None, "", "", False, "Docker is not installed or not available on PATH."
    with _CONTAINER_LOCK:
        _ACTIVE_CONTAINERS.setdefault(execution_id, set()).add(container_name)
    assert process.stdout is not None and process.stderr is not None
    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    stdout_thread = threading.Thread(
        target=_capture_stream, args=(process.stdout, stdout_parts), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_capture_stream, args=(process.stderr, stderr_parts), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    with _CONTAINER_LOCK:
        stop_requested = execution_id in _CANCELLED_EXECUTIONS
    if stop_requested:
        _stop_container(container_name)
    deadline = time.monotonic() + timeout_seconds
    last_stop = 0.0
    cancelled = False
    timed_out = False
    while process.poll() is None:
        with _CONTAINER_LOCK:
            cancelled = execution_id in _CANCELLED_EXECUTIONS
        if cancelled:
            if time.monotonic() - last_stop >= 0.5:
                _stop_container(container_name)
                last_stop = time.monotonic()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            break
        if time.monotonic() >= deadline:
            timed_out = True
            process.kill()
            process.wait()
            break
        time.sleep(0.1)
    stdout_thread.join()
    stderr_thread.join()
    exit_code = process.wait()
    with _CONTAINER_LOCK:
        active = _ACTIVE_CONTAINERS.get(execution_id)
        if active:
            active.discard(container_name)
            if not active:
                _ACTIVE_CONTAINERS.pop(execution_id, None)
    stdout = b"".join(stdout_parts).decode("utf-8", errors="replace")
    stderr = b"".join(stderr_parts).decode("utf-8", errors="replace")
    if timed_out:
        return (
            None,
            stdout,
            stderr,
            True,
            f"Validation command exceeded {timeout_seconds} seconds.",
        )
    if cancelled:
        return None, stdout, stderr, False, "Validation command stopped by operator."
    return (
        exit_code,
        stdout,
        stderr,
        False,
        None,
    )


def _stop_container(container_name: str) -> None:
    try:
        subprocess.run(
            ["docker", "stop", "--time", "2", container_name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _request_execution_stop(execution_id: str) -> None:
    with _CONTAINER_LOCK:
        if execution_id not in _ACTIVE_VALIDATIONS:
            return
        _CANCELLED_EXECUTIONS.add(execution_id)
        containers = tuple(_ACTIVE_CONTAINERS.get(execution_id, set()))
    for name in containers:
        _stop_container(name)


def _clear_execution_stop(execution_id: str) -> None:
    with _CONTAINER_LOCK:
        _CANCELLED_EXECUTIONS.discard(execution_id)
        _ACTIVE_VALIDATIONS.discard(execution_id)


class PayFlowValidationRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        command_timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        self.settings = settings
        self.command_timeout_seconds = command_timeout_seconds

    def cancel_execution(self, execution_id: str) -> None:
        _request_execution_stop(execution_id)

    def begin_execution(self, execution_id: str) -> None:
        with _CONTAINER_LOCK:
            _ACTIVE_VALIDATIONS.add(execution_id)

    def run(
        self,
        task: Task,
        execution: ExecutionRecord,
        workspace: Path,
        *,
        expected_fingerprint: str,
    ) -> ValidationReport:
        try:
            return self._run(
                task,
                execution,
                workspace,
                expected_fingerprint=expected_fingerprint,
            )
        finally:
            _clear_execution_stop(execution.id)

    def _run(
        self,
        task: Task,
        execution: ExecutionRecord,
        workspace: Path,
        *,
        expected_fingerprint: str,
    ) -> ValidationReport:
        started_at = datetime.now(timezone.utc)
        commands = payflow_commands(self.settings)
        results: list[ValidationCommandResult] = []
        summary = "Validation is not configured for this repository."
        overall_status = ValidationStatus.BLOCKED

        target = self.settings.approved_repositories.get(task.repository_key)
        profile_configured = (
            bool(target and target.validation_profile is not None)
            or task.repository_key == self.settings.pay_repository_key
            or bool(
                target
                and target.repo.casefold() == "payflow"
                and (workspace / "backend").is_dir()
                and (workspace / "frontend").is_dir()
            )
        )
        if not profile_configured:
            summary = "Validation skipped: No validation profile is configured for this repository."
            overall_status = ValidationStatus.SKIPPED
            results = [
                self._result(
                    command,
                    ValidationCommandStatus.SKIPPED,
                    error="No validation profile is configured for this repository.",
                )
                for command in commands
            ]
        else:
            try:
                initial_fingerprint = compute_patch_fingerprint(workspace)
                if initial_fingerprint != expected_fingerprint:
                    raise ValueError("workspace patch fingerprint changed before validation")
                with tempfile.TemporaryDirectory(prefix="nexus-validation-") as temporary:
                    staging = Path(temporary) / "source"
                    staging.mkdir()
                    _copy_source(workspace, staging)
                    image_cache: dict[str, bool] = {}
                    for index, command in enumerate(commands):
                        if command.image not in image_cache:
                            image_cache[command.image] = self._image_available(command.image)
                        if image_cache[command.image]:
                            result = self._run_one(command, staging, execution.id)
                        else:
                            result = self._result(
                                command,
                                ValidationCommandStatus.UNAVAILABLE,
                                error="The required prebuilt validator image is unavailable.",
                            )
                        results.append(result)
                        if result.status != ValidationCommandStatus.PASSED:
                            results.extend(
                                self._result(
                                    remaining,
                                    ValidationCommandStatus.UNEXECUTED,
                                    error=(
                                        "Skipped after an earlier validation command did not pass."
                                    ),
                                )
                                for remaining in commands[index + 1 :]
                            )
                            if result.status == ValidationCommandStatus.UNAVAILABLE:
                                summary = (
                                    "Validation blocked because a prebuilt validator image is "
                                    "unavailable."
                                )
                            break
                final_fingerprint = compute_patch_fingerprint(workspace)
                if final_fingerprint != expected_fingerprint:
                    raise ValueError("workspace patch fingerprint changed during validation")
                if all(result.status == ValidationCommandStatus.PASSED for result in results):
                    overall_status = ValidationStatus.PASSED
                    summary = "All configured validation commands passed."
                elif any(
                    result.status
                    in {ValidationCommandStatus.FAILED, ValidationCommandStatus.TIMED_OUT}
                    for result in results
                ):
                    overall_status = ValidationStatus.FAILED
                    summary = "Validation stopped at the first failed or timed out command."
                else:
                    overall_status = ValidationStatus.BLOCKED
                    if summary == "Validation is not configured for this repository.":
                        summary = "Validation stopped because a required check was unavailable."
            except (OSError, ValueError) as exc:
                results.extend(
                    self._result(
                        command,
                        ValidationCommandStatus.UNEXECUTED,
                        error="Validation could not safely stage or verify the workspace.",
                    )
                    for command in commands[len(results) :]
                )
                overall_status = ValidationStatus.BLOCKED
                summary = str(exc)[:2000]

        finished_at = datetime.now(timezone.utc)
        return ValidationReport(
            id=uuid4().hex,
            execution_id=execution.id,
            task_id=task.id,
            patch_fingerprint=expected_fingerprint,
            overall_status=overall_status,
            started_at=started_at,
            finished_at=finished_at,
            commands=results,
            summary=summary,
        )

    def _image_available(self, image: str) -> bool:
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _run_one(
        self, command: ValidationCommand, staging: Path, execution_id: str
    ) -> ValidationCommandResult:
        started_at = datetime.now(timezone.utc)
        monotonic_start = time.monotonic()
        exit_code, stdout, stderr, timed_out, error = _run_container(
            command,
            staging,
            timeout_seconds=self.command_timeout_seconds,
            execution_id=execution_id,
        )
        finished_at = datetime.now(timezone.utc)
        if error and not timed_out:
            status = ValidationCommandStatus.UNAVAILABLE
        elif timed_out:
            status = ValidationCommandStatus.TIMED_OUT
        elif exit_code == 0:
            status = ValidationCommandStatus.PASSED
        else:
            status = ValidationCommandStatus.FAILED
        return ValidationCommandResult(
            command_id=command.command_id,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=exit_code,
            duration_ms=max(0, int((time.monotonic() - monotonic_start) * 1000)),
            stdout_excerpt=_redact_output(stdout[-MAX_CAPTURE_BYTES:]),
            stderr_excerpt=_redact_output(stderr[-MAX_CAPTURE_BYTES:]),
            error=error,
        )

    @staticmethod
    def _result(
        command: ValidationCommand,
        status: ValidationCommandStatus,
        *,
        error: str,
    ) -> ValidationCommandResult:
        return ValidationCommandResult(command_id=command.command_id, status=status, error=error)


__all__ = [
    "PayFlowValidationRunner",
    "ValidationCommand",
    "compute_patch_fingerprint",
    "payflow_commands",
]


def _redact_output(value: str) -> str:
    return _TOKEN_PATTERN.sub("[REDACTED]", redact_credentials(value))[:MAX_CAPTURE_BYTES]
