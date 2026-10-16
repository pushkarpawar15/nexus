from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from google.antigravity import types
from google.antigravity.hooks.hooks import OperationContext, SessionContext, TurnContext
from google.antigravity.hooks.policy import enforce

from app.core.config import Settings
from app.integrations import validation_runner
from app.integrations.validation_runner import (
    AgentPreparationPlan,
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
    dependency_setup=None,
):
    runner = AntigravityValidationRunner(
        Settings(_env_file=None, environment="testing", storage="memory")
    )

    async def ask(_task, _execution, _workspace, **_kwargs):
        return output, tool_results or []

    async def discover(_task, _execution, _workspace):
        return AgentPreparationPlan(summary="Test project discovery", projects=[])

    runner._ask_antigravity = ask
    runner._discover_project_setups = discover
    fingerprint = compute_patch_fingerprint(workspace)
    execution.patch_fingerprint = fingerprint
    original_prepare = validation_runner._prepare_locked_npm_dependencies

    async def prepare(_workspace, **_kwargs):
        return dependency_setup or []

    validation_runner._prepare_locked_npm_dependencies = prepare
    try:
        return runner.run(task, execution, workspace, expected_fingerprint=fingerprint)
    finally:
        validation_runner._prepare_locked_npm_dependencies = original_prepare


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


def test_unexecuted_check_blocks_without_being_classified_as_a_code_failure(
    tmp_path: Path,
) -> None:
    task, execution = records()
    workspace = make_workspace(tmp_path / "workspace")
    output = AgentValidationOutput(
        outcome="BLOCKED",
        summary="npm dependencies could not be installed in the sandbox.",
        checks=[
            AgentValidationCheck(
                name="Vitest",
                command="npm test",
                status="UNEXECUTED",
                summary="The locked dependency install was unavailable.",
                stderr_excerpt="Registry access was denied.",
            )
        ],
    )

    report = run_with_output(task, execution, workspace, output)

    assert report.overall_status == ValidationStatus.BLOCKED
    assert report.commands[0].status == ValidationCommandStatus.UNEXECUTED
    assert report.commands[0].exit_code is None
    assert report.commands[0].error == "Vitest: The locked dependency install was unavailable."
    assert report.commands[0].stderr_excerpt == "Registry access was denied."


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


def test_validation_policy_allows_only_configured_agent_tools() -> None:
    hook = enforce(validation_runner._validation_agent_policies())
    context = OperationContext(TurnContext(SessionContext()))

    async def decide(tool_name: str):
        return await hook.run(context, types.ToolCall(name=tool_name))

    assert asyncio.run(decide(types.BuiltinTools.RUN_COMMAND)).allow is True
    assert asyncio.run(decide(types.BuiltinTools.VIEW_FILE)).allow is True
    assert asyncio.run(decide(types.BuiltinTools.CREATE_FILE)).allow is False


def test_validation_instructions_scope_format_checks_to_changed_files() -> None:
    assert "task.changed_files" in validation_runner._VALIDATION_AGENT_SYSTEM_INSTRUCTIONS
    assert "repository-wide format check" in validation_runner._VALIDATION_AGENT_SYSTEM_INSTRUCTIONS
    assert "Scope formatting checks to changed files" in (
        validation_runner._VALIDATION_TASK_INSTRUCTIONS
    )


def test_validation_command_result_hook_captures_only_run_command_results() -> None:
    tool_results: list[str] = []
    tool_errors: list[str] = []
    hook = validation_runner._validation_command_result_hook(tool_results, tool_errors)
    context = OperationContext(TurnContext(SessionContext()))

    async def record(result: types.ToolResult) -> None:
        await hook.run(context, result)

    asyncio.run(
        record(
            types.ToolResult(
                id="command-1",
                name=types.BuiltinTools.RUN_COMMAND,
                result={"exit_code": 0, "stdout": "All tests passed."},
            )
        )
    )
    asyncio.run(
        record(
            types.ToolResult(
                id="view-1",
                name=types.BuiltinTools.VIEW_FILE,
                result="package.json",
            )
        )
    )
    asyncio.run(
        record(
            types.ToolResult(
                id="command-2",
                name=types.BuiltinTools.RUN_COMMAND,
                error="Command timed out.",
            )
        )
    )

    assert len(tool_results) == 2
    assert "All tests passed." in tool_results[0]
    assert tool_results[1] == "Command timed out."
    assert tool_errors == ["Command timed out."]


def test_repository_discovery_commands_need_not_each_be_a_validation_check() -> None:
    output = AgentValidationOutput(
        outcome="PASSED",
        summary="The repository checks passed.",
        checks=[
            AgentValidationCheck(
                name="pytest",
                command="pytest -q",
                status="PASSED",
                exit_code=0,
                summary="All tests passed.",
            )
        ],
    )

    validation_runner._validate_agent_command_results(output, 4, 4)


def test_repository_can_report_no_checks_after_inspecting_manifests() -> None:
    output = AgentValidationOutput(
        outcome="NO_CHECKS",
        summary="No applicable repository checks were found.",
        checks=[],
    )

    validation_runner._validate_agent_command_results(output, 2, 2)


def test_validation_allows_unrelated_repository_discovery_commands() -> None:
    output = AgentValidationOutput(
        outcome="PASSED",
        summary="The repository checks passed.",
        checks=[
            AgentValidationCheck(
                name="pytest",
                command="pytest -q",
                status="PASSED",
                exit_code=0,
                summary="All tests passed.",
            )
        ],
    )

    validation_runner._validate_agent_command_results(output, 4, 2)


def test_validation_still_requires_a_result_for_each_reported_check() -> None:
    output = AgentValidationOutput(
        outcome="PASSED",
        summary="The repository checks passed.",
        checks=[
            AgentValidationCheck(
                name="pytest",
                command="pytest -q",
                status="PASSED",
                exit_code=0,
                summary="All tests passed.",
            ),
            AgentValidationCheck(
                name="ruff",
                command="ruff check .",
                status="PASSED",
                exit_code=0,
                summary="No lint errors.",
            ),
        ],
    )

    try:
        validation_runner._validate_agent_command_results(output, 4, 1)
    except ValueError as exc:
        assert "2 validation checks" in str(exc)
    else:
        raise AssertionError("Validation must reject incomplete command results.")


def test_blocked_validation_requires_evidence_of_an_environment_attempt() -> None:
    output = AgentValidationOutput(
        outcome="BLOCKED",
        summary="The test runtime is unavailable.",
        checks=[
            AgentValidationCheck(
                name="Vitest",
                command="npm test",
                status="UNEXECUTED",
                summary="npm could not be installed from the lockfile.",
            )
        ],
    )

    validation_runner._validate_agent_command_results(output, 1, 1)
    try:
        validation_runner._validate_agent_command_results(output, 0, 0)
    except ValueError as exc:
        assert "observed 0 command calls and 0 command results" in str(exc)
    else:
        raise AssertionError("A blocked check must include observed environment evidence.")


def test_host_locked_npm_setup_counts_as_blocked_environment_evidence() -> None:
    output = AgentValidationOutput(
        outcome="BLOCKED",
        summary="Frontend checks need npm dependencies that could not be installed.",
        checks=[
            AgentValidationCheck(
                name="Frontend test",
                command="npm test",
                status="UNEXECUTED",
                summary="Locked dependencies were unavailable.",
            )
        ],
    )

    validation_runner._validate_agent_command_results(
        output,
        0,
        0,
        dependency_setup_attempted=True,
    )


def test_locked_npm_dependencies_use_registry_and_ignore_project_npmrc(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = make_workspace(tmp_path / "workspace")
    (workspace / "package-lock.json").write_text(
        '{"lockfileVersion":3,"packages":{"node_modules/vitest":'
        '{"resolved":"https://registry.npmjs.org/vitest/-/vitest-1.0.0.tgz"}}}',
        encoding="utf-8",
    )
    npmrc = workspace / ".npmrc"
    npmrc.write_text("registry=https://untrusted.example\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class CompletedProcess:
        returncode = 0

        async def communicate(self):
            captured["npmrc_hidden_during_install"] = not npmrc.exists()
            return b"added 1 package", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return CompletedProcess()

    monkeypatch.setenv("GITHUB_TOKEN", "must-not-be-forwarded")
    monkeypatch.setattr(
        validation_runner.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    results = asyncio.run(
        validation_runner._prepare_locked_npm_dependencies(
            workspace,
            timeout_seconds=30,
        )
    )

    assert len(results) == 1
    assert results[0].status == ValidationCommandStatus.PASSED
    assert "--ignore-scripts" in results[0].command
    assert "--registry=https://registry.npmjs.org" in results[0].command
    assert "--userconfig=/dev/null" not in results[0].command
    assert "--globalconfig=/dev/null" not in results[0].command
    assert captured["npmrc_hidden_during_install"] is True
    assert npmrc.read_text(encoding="utf-8") == "registry=https://untrusted.example\n"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert "GITHUB_TOKEN" not in kwargs["env"]
    assert kwargs["env"]["npm_config_userconfig"] != kwargs["env"]["npm_config_globalconfig"]


def test_npm_lockfile_rejects_package_urls_outside_public_registry(tmp_path: Path) -> None:
    lockfile = tmp_path / "package-lock.json"
    lockfile.write_text(
        '{"packages":{"node_modules/example":'
        '{"resolved":"https://untrusted.example/example.tgz"}}}',
        encoding="utf-8",
    )

    assert validation_runner._npm_lockfile_error(lockfile) == (
        "npm lockfile references a package outside registry.npmjs.org."
    )


def test_validation_reports_locked_install_failure_as_blocked(tmp_path: Path) -> None:
    task, execution = records()
    workspace = make_workspace(tmp_path / "workspace")
    output = AgentValidationOutput(
        outcome="BLOCKED",
        summary="Frontend checks were not run because npm dependency setup failed.",
        checks=[
            AgentValidationCheck(
                name="Frontend test",
                command="npm test",
                status="UNEXECUTED",
                summary="npm dependencies are unavailable.",
            )
        ],
    )
    setup_result = validation_runner.ValidationCommandResult(
        command_id="npm-ci-frontend",
        command="npm ci --ignore-scripts --no-audit --no-fund",
        status=ValidationCommandStatus.FAILED,
        exit_code=1,
        error="Locked npm dependency installation failed.",
    )

    report = run_with_output(
        task,
        execution,
        workspace,
        output,
        dependency_setup=[setup_result],
    )

    assert report.overall_status == ValidationStatus.BLOCKED
    assert report.commands[0].command_id == "npm-ci-frontend"
    assert report.commands[0].status == ValidationCommandStatus.FAILED
    assert report.commands[1].status == ValidationCommandStatus.UNEXECUTED


def test_validation_cannot_claim_checks_without_running_commands() -> None:
    output = AgentValidationOutput(
        outcome="PASSED",
        summary="The repository checks passed.",
        checks=[
            AgentValidationCheck(
                name="pytest",
                command="pytest -q",
                status="PASSED",
                exit_code=0,
                summary="All tests passed.",
            )
        ],
    )

    try:
        validation_runner._validate_agent_command_results(output, 0, 0)
    except ValueError as exc:
        assert "observed 0 command calls and 0 command results" in str(exc)
    else:
        raise AssertionError("Validation must reject checks with no command execution.")
