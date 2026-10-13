from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.core.config import Settings, validate_settings
from app.integrations.validation_runner import compute_patch_fingerprint
from app.main import create_app
from app.models.task import (
    Complexity,
    ExecutionMode,
    ExecutionPlan,
    ModelTaskAnalysis,
    PlanSource,
    RiskLevel,
    Task,
    TaskStatus,
    TaskType,
)
from app.policies.workforce import WorkforcePolicy
from app.repositories.records import create_memory_workflow_repositories
from app.repositories.tasks import MemoryTaskRepository
from app.schemas.security import SecurityReview, SecurityStatus
from app.schemas.validation import ValidationReport, ValidationStatus
from app.services.execution_pipeline import ExecutionPipeline, LocalExecutionDispatcher
from app.services.execution_service import ExecutionService


def test_policy_does_not_grant_coding_without_both_worker_permissions() -> None:
    decision = WorkforcePolicy().evaluate(
        risk=RiskLevel.LOW,
        task_type=TaskType.BUGFIX,
        complexity=Complexity.LOW,
        available_capabilities={"plan", "ai_execute", "ai_validate"},
        permissions={"ai_execute"},
        recommended_mode=ExecutionMode.AI,
    )
    assert decision.mode == ExecutionMode.HUMAN
    assert "coder" not in decision.allowed_workers
    assert "validator" not in decision.allowed_workers


def test_critical_risk_overrides_permissive_model_recommendation() -> None:
    decision = WorkforcePolicy().evaluate(
        risk=RiskLevel.CRITICAL,
        task_type=TaskType.BUGFIX,
        complexity=Complexity.LOW,
        available_capabilities={"plan", "ai_execute", "ai_validate", "security_review"},
        permissions={"ai_execute", "ai_validate", "security_review"},
        recommended_mode=ExecutionMode.AI,
    )
    assert decision.mode == ExecutionMode.HUMAN
    assert decision.approval_required is True
    assert decision.security_review_required is True
    assert "coder" not in decision.allowed_workers


def test_analysis_route_persists_policy_enforced_plan_and_activity() -> None:
    settings = Settings(
        _env_file=None,
        environment="testing",
        storage="memory",
        firebase_project_id="demo-nexus",
        google_cloud_project="demo-nexus",
        approved_repositories={"sample": {"owner": "example-org", "repo": "sample"}},
        ai_permissions_raw="ai_execute,ai_validate,security_review",
    )
    app = create_app(settings=settings)

    class FakeAnalyzer:
        async def analyze(self, *_args):
            return ModelTaskAnalysis(
                risk=RiskLevel.LOW,
                complexity=Complexity.LOW,
                mode=ExecutionMode.AI,
                assigned_agents=["coder", "validator"],
                approval_required=False,
                reason="A bounded bug fix.",
                steps=["Inspect the parser.", "Add a regression check."],
                validation_plan=["Run the fixed validation profile."],
            )

    app.state.analysis_service.analyzer = FakeAnalyzer()
    app.state.firebase_token_verifier = lambda _token, _settings: {
        "uid": "operator-1",
        "nexus_role": "operator",
    }
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer local-token"}
        created = client.post(
            "/api/tasks",
            headers=headers,
            json={
                "title": "Fix parser edge case",
                "description": "Handle an empty token sequence.",
                "repository_key": "sample",
                "task_type": "BUGFIX",
            },
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        analyzed = client.post(f"/api/tasks/{task_id}/analyze", headers=headers)
        assert analyzed.status_code == 200, analyzed.text
        payload = analyzed.json()
        assert payload["status"] == "READY"
        assert payload["execution_plan"]["source"] == "GEMINI"
        assert payload["assigned_agents"] == ["coder", "validator"]
        assert len(payload["transition_history"]) == 3
        history = client.get(f"/api/tasks/{task_id}/history", headers=headers)
        assert [event["event_type"] for event in history.json()] == [
            "analysis.started",
            "analysis.completed",
        ]


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_local_pipeline_persists_observed_stage_results_and_keeps_target_clean(tmp_path) -> None:
    async def scenario():
        target_path = tmp_path / "payflow"
        target_path.mkdir()
        _git(target_path, "init", "-b", "main")
        _git(target_path, "config", "user.name", "Test")
        _git(target_path, "config", "user.email", "test@example.test")
        (target_path / "README.md").write_text("original\n", encoding="utf-8")
        _git(target_path, "add", "README.md")
        _git(target_path, "commit", "-m", "base")
        _git(target_path, "remote", "add", "origin", "https://github.com/example-org/payflow.git")
        initial_sha = _git(target_path, "rev-parse", "HEAD")

        settings = Settings(
            _env_file=None,
            environment="testing",
            storage="memory",
            google_cloud_project="demo-nexus",
            firebase_project_id="demo-nexus",
            approved_repositories={
                "nexus_pay": {
                    "owner": "example-org",
                    "repo": "payflow",
                    "source_path": target_path,
                }
            },
            coding_enabled=True,
            validation_enabled=True,
            ai_permissions_raw="ai_execute,ai_validate,security_review",
            workspace_root=tmp_path / "private-workspaces",
        )
        validate_settings(settings)
        tasks = MemoryTaskRepository()
        records = create_memory_workflow_repositories()
        now = datetime.now(timezone.utc)
        plan = ExecutionPlan(
            risk=RiskLevel.LOW,
            complexity=Complexity.LOW,
            mode=ExecutionMode.AI,
            assigned_agents=["coder", "validator"],
            approval_required=False,
            reason="Bounded documentation change.",
            steps=["Update README."],
            validation_plan=["Run configured checks."],
            source=PlanSource.GEMINI,
            model_id="test-model",
            policy_reason="Low-risk test task.",
            security_review_required=False,
        )
        task = Task(
            id="task-1",
            title="Clarify project overview",
            description="Update the project README.",
            repository_key="nexus_pay",
            task_type=TaskType.DOCS,
            status=TaskStatus.READY,
            risk=RiskLevel.LOW,
            complexity=Complexity.LOW,
            mode=ExecutionMode.AI,
            assigned_agents=["coder", "validator"],
            execution_plan=plan,
            approval_required=False,
            created_at=now,
            updated_at=now,
            created_by="operator-1",
        )
        await tasks.create(task)

        class FakeCoder:
            async def edit(self, _task, workspace, **_kwargs):
                (workspace.path / "README.md").write_text(
                    "updated by isolated worker\n", encoding="utf-8"
                )
                fingerprint = compute_patch_fingerprint(workspace.path)
                return ["README.md"], fingerprint, "1 file changed, 1 addition, 1 deletion."

        class FakeValidator:
            async def validate(self, current_task, execution, _workspace):
                report = ValidationReport(
                    id="validation-1",
                    execution_id=execution.id,
                    task_id=current_task.id,
                    patch_fingerprint=execution.patch_fingerprint,
                    overall_status=ValidationStatus.PASSED,
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                    commands=[],
                    summary="Fake validator explicitly observed a passing test boundary.",
                )
                await records.validation_reports.create(report)
                return report

        class FakeSecurity:
            async def review(self, current_task, execution, _workspace, _validation):
                report = SecurityReview(
                    id="security-1",
                    execution_id=execution.id,
                    task_id=current_task.id,
                    patch_fingerprint=execution.patch_fingerprint,
                    overall_status=SecurityStatus.PASSED,
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc),
                    findings=[],
                    review_limitations=["Test fake; no model review was run."],
                    summary="Fake security boundary returned a passing result.",
                )
                await records.security_reviews.create(report)
                return report

        pipeline = ExecutionPipeline(
            settings,
            tasks,
            records,
            coder=FakeCoder(),
            validator=FakeValidator(),
            security=FakeSecurity(),
        )
        dispatcher = LocalExecutionDispatcher(pipeline)
        service = ExecutionService(settings, tasks, records, dispatcher)
        queued = await service.start(task, "operator-1")
        for _ in range(100):
            await asyncio.sleep(0.01)
            completed = await records.executions.get(queued.id)
            if completed and completed.status.value in {"SUCCEEDED", "FAILED", "BLOCKED"}:
                break
        worker = dispatcher._tasks.get(queued.id)
        if worker is not None:
            await worker
        await dispatcher.close()
        final_task = await tasks.get(task.id)
        final_execution = await records.executions.get(queued.id)
        assert final_task is not None and final_task.status == TaskStatus.APPROVED
        assert final_execution is not None and final_execution.status.value == "SUCCEEDED"
        assert final_execution.patch_fingerprint
        assert await records.validation_reports.get("validation-1") is not None
        assert await records.security_reviews.get("security-1") is not None
        assert _git(target_path, "rev-parse", "HEAD") == initial_sha
        assert not _git(target_path, "status", "--porcelain")

    asyncio.run(scenario())
