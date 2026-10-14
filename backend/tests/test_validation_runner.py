from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings
from app.integrations import validation_runner
from app.integrations.validation_runner import (
    AgentValidationCheck,
    AgentValidationOutput,
    AntigravityValidationRunner,
    compute_patch_fingerprint,
)
from app.models.task import Task, TaskStatus
from app.schemas.execution import ExecutionRecord, ExecutionStatus
from app.schemas.validation import ValidationCommandStatus, ValidationStatus


def records() -> tuple[Task, ExecutionRecord]:
    now = datetime.now(timezone.utc)
    task = Task(
        id="task-1",
        title="A validation task",
        description="Run available repository checks",
        repository_key="github-123",
        status=TaskStatus.VALIDATING,
        created_at=now,
        updated_at=now,
        created_by="operator-1",
    )
    execution = ExecutionRecord(
        id="execution-1",
        task_id=task.id,
        assigned_worker="coding-agent",
        status=ExecutionStatus.RUNNING,
        created_at=now,
        updated_at=now,
        repository_key=task.repository_key,
        patch_fingerprint="0" * 64,
        changed_files=["src/app.ts"],
    )
    return task, execution


def make_workspace(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text('{"scripts":{"test":"vitest run"}}\n', encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "app.ts").write_text("export const answer = 42;\n", encoding="utf-8")
    return root


def run_with_output(
    task: Task,
    execution: ExecutionRecord,
    workspace: Path,
    output: AgentValidationOutput,
    *,
    tool_results: list[str] | None = None,
):
    runner = AntigravityValidationRunner(
        Settings(_env_file=None, environment="testing", storage="memory")
    )

    async def ask(_task, _execution, _workspace):
        return output, tool_results or []

    runner._ask_antigravity = ask
    fingerprint = compute_patch_fingerprint(workspace)
    execution.patch_fingerprint = fingerprint
    return runner.run(task, execution, workspace, expected_fingerprint=fingerprint)


def test_validation_discovers_repository_checks_without_a_profile(tmp_path: Path) -> None:
    task, execution = records()
    workspace = make_workspace(tmp_path / "workspace")
    output = AgentValidationOutput(
        outcome="PASSED",
        summary="The repository test suite passed.",
        checks=[
            AgentValidationCheck(
                name="Vitest",
                command="npm test -- --run",
                status="PASSED",
                exit_code=0,
                summary="All tests passed.",
                stdout_excerpt="Tests 4 passed",
            )
        ],
    )

    report = run_with_output(task, execution, workspace, output, tool_results=["exit code 0"])

    assert report.overall_status == ValidationStatus.PASSED
    assert report.summary == "The repository test suite passed."
    assert len(report.commands) == 1
    assert report.commands[0].command_id == "Vitest"
    assert report.commands[0].command == "npm test -- --run"
    assert report.commands[0].status == ValidationCommandStatus.PASSED
    assert report.commands[0].stdout_excerpt == "Tests 4 passed\nexit code 0"
    assert report.commands[0].stderr_excerpt == ""


def test_no_applicable_repository_checks_remains_a_blocking_skip(tmp_path: Path) -> None:
    task, execution = records()
    workspace = make_workspace(tmp_path / "workspace")
    output = AgentValidationOutput(
        outcome="NO_CHECKS",
        summary="No repository test or quality-check commands were found.",
        checks=[],
    )

    report = run_with_output(task, execution, workspace, output)

    assert report.overall_status == ValidationStatus.SKIPPED
    assert report.summary.startswith("Validation skipped:")
    assert report.commands[0].status == ValidationCommandStatus.SKIPPED


def test_failed_check_cannot_be_marked_passed_with_nonzero_exit_code(tmp_path: Path) -> None:
    task, execution = records()
    workspace = make_workspace(tmp_path / "workspace")
    output = AgentValidationOutput(
        outcome="PASSED",
        summary="Validation finished.",
        checks=[
            AgentValidationCheck(
                name="Vitest",
                command="npm test",
                status="PASSED",
                exit_code=1,
                summary="One test failed.",
            )
        ],
    )

    report = run_with_output(task, execution, workspace, output)

    assert report.overall_status == ValidationStatus.FAILED
    assert report.commands[0].status == ValidationCommandStatus.FAILED
    assert report.commands[0].error == "Vitest: One test failed."


def test_validation_preserves_the_original_workspace_fingerprint(tmp_path: Path) -> None:
    task, execution = records()
    workspace = make_workspace(tmp_path / "workspace")
    before = compute_patch_fingerprint(workspace)
    output = AgentValidationOutput(
        outcome="PASSED",
        summary="Repository checks passed.",
        checks=[
            AgentValidationCheck(
                name="pytest",
                command="pytest",
                status="PASSED",
                exit_code=0,
                summary="All tests passed.",
            )
        ],
    )

    report = run_with_output(task, execution, workspace, output)

    assert report.overall_status == ValidationStatus.PASSED
    assert compute_patch_fingerprint(workspace) == before
    assert validation_runner.MAX_TOTAL_SOURCE_BYTES == 30 * 1024 * 1024
