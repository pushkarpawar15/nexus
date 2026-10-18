from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from google.antigravity import types
from google.antigravity.hooks.hooks import OperationContext, SessionContext, TurnContext
from google.antigravity.hooks.policy import enforce

from app.core.config import Settings
from app.integrations import validation_runner
from app.integrations.validation_runner import (
    CHECKS_MAX_TOOL_CALLS,
    DISCOVERY_MAX_MODEL_CALLS,
    DISCOVERY_MAX_TOOL_CALLS,
    MODEL_API_MAX_RETRIES,
    AgentPreparationPlan,
    AgentValidationCheck,
    AgentValidationOutput,
    AntigravityValidationRunner,
    _request_preparation_plan,
    _request_validation_output,
    _run_isolated_antigravity_call,
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


def _hang_in_antigravity_worker() -> None:
    while True:
        time.sleep(1)


def _exit_without_antigravity_result() -> None:
    os._exit(17)


def _return_agent_validation_output() -> AgentValidationOutput:
    return AgentValidationOutput(
        outcome="NO_CHECKS", summary="No checks were applicable.", checks=[]
    )


def test_isolated_worker_restores_installed_module_paths() -> None:
    async def run() -> None:
        result = await _run_isolated_antigravity_call(
            _return_agent_validation_output,
            timeout_seconds=5,
            stage="project discovery",
        )
        assert result.outcome == "NO_CHECKS"
        assert result.checks == []

    asyncio.run(run())


def test_isolated_worker_bootstraps_when_parent_site_paths_are_missing() -> None:
    probe = Path(__file__).parent / "support" / "spawn_bootstrap_probe.py"
    result = subprocess.run(
        [sys.executable, str(probe)],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "pydantic=" in result.stdout


def test_missing_antigravity_structured_output_retries_once() -> None:
    class FakeResponse:
        def __init__(self, structured):
            self.structured = structured

        async def resolve(self):
            return []

        async def structured_output(self):
            return self.structured

    class FakeAgent:
        def __init__(self):
            self.prompts = []
            self.responses = [
                FakeResponse(None),
                FakeResponse(
                    {"outcome": "NO_CHECKS", "summary": "No checks were applicable.", "checks": []}
                ),
            ]

        async def chat(self, prompt):
            self.prompts.append(prompt)
            return self.responses.pop(0)

    async def run() -> None:
        agent = FakeAgent()
        output, chunks = await _request_validation_output(agent, "check the repository")
        assert output.outcome == "NO_CHECKS"
        assert chunks == []
        assert len(agent.prompts) == 2
        assert "required structured validation result" in agent.prompts[1]

    asyncio.run(run())


def test_missing_antigravity_structured_output_returns_clear_error() -> None:
    class FakeResponse:
        stop_reason = "STOP"

        async def resolve(self):
            return []

        async def structured_output(self):
            return None

    class FakeAgent:
        async def chat(self, _prompt):
            return FakeResponse()

    async def run() -> None:
        with pytest.raises(
            RuntimeError,
            match="ended without a structured validation result after one retry",
        ):
            await _request_validation_output(FakeAgent(), "check the repository")

    asyncio.run(run())


def test_missing_antigravity_preparation_plan_retries_once() -> None:
    class FakeResponse:
        def __init__(self, structured):
            self.structured = structured

        async def resolve(self):
            return []

        async def structured_output(self):
            return self.structured

    class FakeAgent:
        def __init__(self):
            self.prompts = []
            self.responses = [
                FakeResponse(None),
                FakeResponse({"summary": "One project.", "projects": []}),
            ]

        async def chat(self, prompt):
            self.prompts.append(prompt)
            return self.responses.pop(0)

    async def run() -> None:
        agent = FakeAgent()
        plan = await _request_preparation_plan(agent, "inspect workspace manifests")
        assert plan.summary == "One project."
        assert len(agent.prompts) == 2
        assert "structured project setup plan" in agent.prompts[1]

    asyncio.run(run())


def test_missing_antigravity_preparation_plan_returns_clear_error() -> None:
    class FakeResponse:
        stop_reason = "MAX_TOOL_CALLS_EXCEEDED"

        async def resolve(self):
            return []

        async def structured_output(self):
            return None

    class FakeAgent:
        async def chat(self, _prompt):
            return FakeResponse()

    async def run() -> None:
        with pytest.raises(
            RuntimeError,
            match="discovery ended without a structured setup plan after one retry",
        ):
            await _request_preparation_plan(FakeAgent(), "inspect workspace manifests")

    asyncio.run(run())


def test_project_discovery_config_bounds_budget_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import google.antigravity as real_antigravity

    captured: dict[str, object] = {}

    class FakeResponse:
        async def resolve(self):
            return []

        async def structured_output(self):
            return {"summary": "Inventoried the manifests.", "projects": []}

    class FakeAgent:
        sandbox_status = SimpleNamespace(available=True, unavailable_reason=None)

        def __init__(self, config):
            captured["agent_config"] = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def chat(self, _content):
            return FakeResponse()

    class FakeLocalAgentConfig:
        def __init__(self, **kwargs):
            captured["config"] = kwargs

    fake_sdk = ModuleType("google.antigravity")
    fake_sdk.Agent = FakeAgent
    fake_sdk.LocalAgentConfig = FakeLocalAgentConfig
    fake_sdk.types = real_antigravity.types
    monkeypatch.setitem(sys.modules, "google.antigravity", fake_sdk)

    task, execution = records()
    runner = AntigravityValidationRunner(
        Settings(
            _env_file=None,
            environment="testing",
            storage="memory",
            google_cloud_project="demo-nexus",
        )
    )

    plan = asyncio.run(runner._discover_project_setups_with_sdk(task, execution, tmp_path))

    assert plan.summary == "Inventoried the manifests."
    config = captured["config"]
    assert config["budget_config"].max_tool_calls == DISCOVERY_MAX_TOOL_CALLS
    assert config["budget_config"].max_model_calls == DISCOVERY_MAX_MODEL_CALLS
    assert config["retry_config"].api_retry.max_retries == MODEL_API_MAX_RETRIES


def test_validation_checks_config_bounds_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import google.antigravity as real_antigravity

    captured: dict[str, object] = {}

    class FakeResponse:
        async def resolve(self):
            return []

        async def structured_output(self):
            return {
                "outcome": "NO_CHECKS",
                "summary": "No checks were applicable.",
                "checks": [],
            }

    class FakeAgent:
        sandbox_status = SimpleNamespace(available=True, unavailable_reason=None)

        def __init__(self, config):
            captured["agent_config"] = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def chat(self, _content):
            return FakeResponse()

    class FakeLocalAgentConfig:
        def __init__(self, **kwargs):
            captured["config"] = kwargs

    fake_sdk = ModuleType("google.antigravity")
    fake_sdk.Agent = FakeAgent
    fake_sdk.LocalAgentConfig = FakeLocalAgentConfig
    fake_sdk.types = real_antigravity.types
    monkeypatch.setitem(sys.modules, "google.antigravity", fake_sdk)

    task, execution = records()
    runner = AntigravityValidationRunner(
        Settings(
            _env_file=None,
            environment="testing",
            storage="memory",
            google_cloud_project="demo-nexus",
        )
    )

    output, _chunks = asyncio.run(runner._ask_antigravity_with_sdk(task, execution, tmp_path))

    assert output.outcome == "NO_CHECKS"
    config = captured["config"]
    assert config["budget_config"].max_tool_calls == CHECKS_MAX_TOOL_CALLS
    assert config["retry_config"].api_retry.max_retries == MODEL_API_MAX_RETRIES


def test_antigravity_startup_timeout_kills_isolated_worker_process() -> None:
    async def run() -> None:
        with pytest.raises(
            validation_runner.AntigravityTimeoutError,
            match="project discovery exceeded its 1-second timeout",
        ):
            await _run_isolated_antigravity_call(
                _hang_in_antigravity_worker,
                timeout_seconds=1,
                stage="project discovery",
            )

    asyncio.run(run())


def test_antigravity_worker_exit_without_result_has_actionable_error() -> None:
    async def run() -> None:
        with pytest.raises(
            RuntimeError,
            match=r"worker closed its result channel without a response \(exit code 17\)",
        ):
            await _run_isolated_antigravity_call(
                _exit_without_antigravity_result,
                timeout_seconds=5,
                stage="project discovery",
            )

    asyncio.run(run())


def test_antigravity_timeout_is_reported_as_timed_out(tmp_path: Path) -> None:
    task, execution = records()
    workspace = make_workspace(tmp_path / "workspace")
    runner = AntigravityValidationRunner(
        Settings(_env_file=None, environment="testing", storage="memory")
    )

    async def timeout(_task, _execution, _workspace):
        raise validation_runner.AntigravityTimeoutError(
            "Antigravity project discovery exceeded its 600-second timeout."
        )

    runner._discover_project_setups = timeout
    fingerprint = compute_patch_fingerprint(workspace)
    execution.patch_fingerprint = fingerprint

    report = runner.run(task, execution, workspace, expected_fingerprint=fingerprint)

    assert report.overall_status == ValidationStatus.BLOCKED
    assert report.commands[0].status == ValidationCommandStatus.TIMED_OUT
    assert "project discovery" in report.summary


def test_scan_manifest_projects_finds_project_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname = 'nexus'\n", encoding="utf-8")
    frontend = workspace / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text('{"name": "ui"}\n', encoding="utf-8")
    (frontend / "package-lock.json").write_text("{}\n", encoding="utf-8")
    ignored = workspace / "node_modules"
    ignored.mkdir()
    (ignored / "package.json").write_text('{"name": "dep"}\n', encoding="utf-8")
    venv = workspace / ".venv"
    venv.mkdir()
    (venv / "pyproject.toml").write_text("[project]\nname = 'venv'\n", encoding="utf-8")

    plan = validation_runner._scan_manifest_projects(workspace)

    assert {(project.project_root, project.ecosystem) for project in plan.projects} == {
        (".", "Python"),
        ("frontend", "Node.js"),
    }
    python_roots, node_roots, inventory = AntigravityValidationRunner._validated_project_roots(
        workspace, plan
    )
    assert python_roots == [workspace.resolve()]
    assert node_roots == [frontend.resolve()]
    assert len(inventory) == 2


@pytest.mark.parametrize(
    ("failure", "marker"),
    [
        (
            validation_runner.AntigravityTimeoutError(
                "Antigravity project discovery exceeded its 180-second timeout."
            ),
            "180-second timeout",
        ),
        (
            RuntimeError("Project discovery blocked: sandbox unavailable."),
            "sandbox unavailable",
        ),
    ],
)
def test_project_discovery_falls_back_to_manifest_scan(
    failure: BaseException,
    marker: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task, execution = records()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname = 'nexus'\n", encoding="utf-8")
    runner = AntigravityValidationRunner(
        Settings(_env_file=None, environment="testing", storage="memory")
    )
    observed: dict[str, object] = {}

    async def fail(*_args, **kwargs):
        observed.update(kwargs)
        raise failure

    monkeypatch.setattr(validation_runner, "_run_isolated_antigravity_call", fail)

    plan = asyncio.run(runner._discover_project_setups(task, execution, workspace))

    assert observed["stage"] == "project discovery"
    assert observed["timeout_seconds"] == validation_runner.PROJECT_DISCOVERY_TIMEOUT_SECONDS
    assert [project.project_root for project in plan.projects] == ["."]
    assert "scanned directly instead" in plan.summary
    assert marker in plan.summary


def test_run_recovers_when_project_discovery_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task, execution = records()
    workspace = make_workspace(tmp_path / "workspace")
    fingerprint = compute_patch_fingerprint(workspace)
    execution.patch_fingerprint = fingerprint
    runner = AntigravityValidationRunner(
        Settings(_env_file=None, environment="testing", storage="memory")
    )
    stages: list[str] = []

    async def fake_isolated(_target, *_args, timeout_seconds, stage, **_kwargs):
        stages.append(stage)
        if stage == "project discovery":
            raise validation_runner.AntigravityTimeoutError(
                "Antigravity project discovery exceeded its 180-second timeout."
            )
        return (
            AgentValidationOutput(
                outcome="NO_CHECKS", summary="No checks were applicable.", checks=[]
            ),
            [],
        )

    monkeypatch.setattr(validation_runner, "_run_isolated_antigravity_call", fake_isolated)

    report = runner.run(task, execution, workspace, expected_fingerprint=fingerprint)

    assert stages == ["project discovery", "repository checks"]
    assert report.overall_status == ValidationStatus.SKIPPED
    assert "Validation skipped" in report.summary
    assert all(command.status != ValidationCommandStatus.TIMED_OUT for command in report.commands)


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
