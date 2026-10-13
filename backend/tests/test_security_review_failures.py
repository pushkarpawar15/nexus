from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

from app.core.config import Settings
from app.integrations.security_gemini import GeminiSecurityReviewer
from app.integrations.validation_runner import compute_patch_fingerprint
from app.models.task import Task
from app.repositories.records import create_memory_workflow_repositories
from app.schemas.security import ModelSecurityOutput, SecurityStatus
from app.schemas.validation import ValidationReport, ValidationStatus
from app.services.security_service import SecurityService


def test_review_failure_is_diagnosable_and_redacts_credentials(tmp_path, caplog) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=workspace,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=workspace,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.test"],
            cwd=workspace,
            check=True,
            capture_output=True,
        )
        readme = workspace / "README.md"
        readme.write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=workspace, check=True)
        subprocess.run(
            ["git", "commit", "-m", "baseline"],
            cwd=workspace,
            check=True,
            capture_output=True,
        )
        readme.write_text("after\n", encoding="utf-8")

        now = datetime.now(timezone.utc)
        fingerprint = compute_patch_fingerprint(workspace)
        task = Task(
            id="task-1",
            title="Test security review diagnostics",
            description="Verify contextual reviewer failures are visible.",
            repository_key="sample",
            created_at=now,
            updated_at=now,
            created_by="operator-1",
        )
        execution = SimpleNamespace(id="execution-1", patch_fingerprint=fingerprint)
        validation = ValidationReport(
            id="validation-1",
            execution_id=execution.id,
            task_id=task.id,
            patch_fingerprint=fingerprint,
            overall_status=ValidationStatus.PASSED,
            started_at=now,
            finished_at=now,
            commands=[],
            summary="Validation passed.",
        )
        leaked_value = "github_pat_" + "A" * 40

        class FailingReviewer:
            async def review(self, *_args):
                raise RuntimeError(f"provider rejected credential {leaked_value}")

        records = create_memory_workflow_repositories()
        service = SecurityService(Settings(_env_file=None), records, FailingReviewer())

        report = await service.review(task, execution, workspace, validation)

        assert report.overall_status == SecurityStatus.BLOCKED
        assert any("RuntimeError" in item for item in report.review_limitations)
        assert any("[REDACTED]" in item for item in report.review_limitations)
        assert leaked_value not in " ".join(report.review_limitations)
        assert leaked_value not in caplog.text
        assert await records.security_reviews.get(report.id) == report

    asyncio.run(scenario())


def test_gemini_security_reviewer_retries_transient_provider_failure(monkeypatch) -> None:
    async def scenario() -> None:
        calls = 0
        delays: list[int] = []

        class ProviderUnavailable(Exception):
            status_code = 503

        class FakeModels:
            async def generate_content(self, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise ProviderUnavailable()
                return SimpleNamespace(parsed=ModelSecurityOutput())

        async def fake_sleep(delay: int) -> None:
            delays.append(delay)

        monkeypatch.setattr("app.integrations.security_gemini.asyncio.sleep", fake_sleep)
        reviewer = GeminiSecurityReviewer(
            Settings(_env_file=None, google_cloud_project="demo-project"),
            client=SimpleNamespace(aio=SimpleNamespace(models=FakeModels())),
        )

        result = await reviewer.review(SimpleNamespace(title="Test task"), "diff", [])

        assert result == ModelSecurityOutput()
        assert calls == 2
        assert delays == [1]

    asyncio.run(scenario())
