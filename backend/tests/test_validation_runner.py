from __future__ import annotations

import io
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings
from app.integrations import validation_runner
from app.integrations.validation_runner import (
    PayFlowValidationRunner,
    compute_patch_fingerprint,
    payflow_commands,
)
from app.models.task import Task, TaskStatus
from app.schemas.execution import ExecutionRecord, ExecutionStatus
from app.schemas.validation import ValidationCommandStatus, ValidationStatus


def records() -> tuple[Task, ExecutionRecord]:
    now = datetime.now(timezone.utc)
    task = Task(
        id="task-1",
        title="A validation task",
        description="Run configured checks",
        repository_key="nexus_pay",
        status=TaskStatus.IN_PROGRESS,
        created_at=now,
        updated_at=now,
        created_by="operator-1",
    )
    execution = ExecutionRecord(
        id="execution-1",
        task_id=task.id,
        assigned_worker="coding-agent",
        status=ExecutionStatus.QUEUED,
        created_at=now,
        updated_at=now,
        repository_key=task.repository_key,
    )
    return task, execution


def make_workspace(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "backend").mkdir()
    (root / "frontend").mkdir()
    (root / "backend" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "frontend" / "package.json").write_text("{}\n", encoding="utf-8")
    return root


def test_payflow_profile_uses_fixed_order_and_arguments() -> None:
    settings = Settings(_env_file=None, environment="testing", storage="memory")

    commands = payflow_commands(settings)

    assert [command.command_id for command in commands] == [
        "backend_pytest",
        "backend_ruff",
        "backend_format",
        "frontend_test",
        "frontend_lint",
        "frontend_typecheck",
        "frontend_build",
    ]
    assert [command.argv for command in commands[-4:]] == [
        ("npm", "test", "--", "--run"),
        ("npm", "run", "lint"),
        ("npm", "run", "typecheck"),
        ("npm", "run", "build"),
    ]


def test_patch_fingerprint_ignores_environment_files_and_changes_with_source(
    tmp_path: Path,
) -> None:
    workspace = make_workspace(tmp_path)
    first = compute_patch_fingerprint(workspace)
    (workspace / "backend" / ".env.local").write_text("SECRET=ignored", encoding="utf-8")
    assert compute_patch_fingerprint(workspace) == first
    (workspace / "backend" / "main.py").write_text("print('changed')\n", encoding="utf-8")
    assert compute_patch_fingerprint(workspace) != first


def test_missing_image_marks_first_check_unavailable_and_skips_the_rest(
    tmp_path: Path, monkeypatch
) -> None:
    settings = Settings(_env_file=None, environment="testing", storage="memory")
    task, execution = records()
    workspace = make_workspace(tmp_path / "workspace")
    monkeypatch.setattr(
        validation_runner.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", "missing"),
    )

    report = PayFlowValidationRunner(settings).run(
        task,
        execution,
        workspace,
        expected_fingerprint=compute_patch_fingerprint(workspace),
    )

    assert report.overall_status == ValidationStatus.BLOCKED
    assert report.commands[0].status == ValidationCommandStatus.UNAVAILABLE
    assert all(
        command.status == ValidationCommandStatus.UNEXECUTED for command in report.commands[1:]
    )


def test_failed_command_stops_the_profile(tmp_path: Path, monkeypatch) -> None:
    class FailedContainer:
        stdout = io.BytesIO(b"test output")
        stderr = io.BytesIO(b"failure details")

        def wait(self, timeout=None):
            return 1

    settings = Settings(_env_file=None, environment="testing", storage="memory")
    task, execution = records()
    workspace = make_workspace(tmp_path / "workspace")
    monkeypatch.setattr(
        validation_runner.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
    )
    monkeypatch.setattr(
        validation_runner.subprocess,
        "Popen",
        lambda *args, **kwargs: FailedContainer(),
    )

    report = PayFlowValidationRunner(settings).run(
        task,
        execution,
        workspace,
        expected_fingerprint=compute_patch_fingerprint(workspace),
    )

    assert report.overall_status == ValidationStatus.FAILED
    assert report.commands[0].status == ValidationCommandStatus.FAILED
    assert report.commands[0].stdout_excerpt == "test output"
    assert all(
        command.status == ValidationCommandStatus.UNEXECUTED for command in report.commands[1:]
    )
