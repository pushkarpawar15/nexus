from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings
from app.integrations.validation_runner import compute_patch_fingerprint
from app.models.task import Task, TaskStatus
from app.repositories.records import create_memory_workflow_repositories
from app.repositories.tasks import MemoryTaskRepository
from app.schemas.execution import ExecutionRecord, ExecutionStatus
from app.schemas.validation import (
    ValidationCommandResult,
    ValidationCommandStatus,
    ValidationReport,
    ValidationStatus,
)
from app.services.validation_service import ValidationService


def test_validation_service_retries_only_transient_antigravity_unavailable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime.now(timezone.utc)
        task = Task(
            id="task-validation-retry",
            title="Validate a repository change",
            description="Run repository checks",
            repository_key="github-123",
            status=TaskStatus.VALIDATING,
            created_at=now,
            updated_at=now,
            created_by="operator-1",
        )
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "README.md").write_text("validation target\n", encoding="utf-8")
        fingerprint = compute_patch_fingerprint(workspace)
        execution = ExecutionRecord(
            id="execution-validation-retry",
            task_id=task.id,
            assigned_worker="coding-agent",
            status=ExecutionStatus.RUNNING,
            created_at=now,
            updated_at=now,
            repository_key=task.repository_key,
            patch_fingerprint=fingerprint,
        )

        def report(status: ValidationStatus, command_status: ValidationCommandStatus):
            return ValidationReport(
                id=f"validation-{status.value.lower()}",
                execution_id=execution.id,
                task_id=task.id,
                patch_fingerprint=fingerprint,
                overall_status=status,
                started_at=now,
                finished_at=now,
                commands=[
                    ValidationCommandResult(
                        command_id="antigravity_validation",
                        status=command_status,
                        error=("temporary startup issue" if command_status != "PASSED" else None),
                    )
                ],
                summary=f"Antigravity returned {command_status.value}.",
            )

        class FlakyRunner:
            calls = 0

            def begin_execution(self, _execution_id: str) -> None:
                pass

            def run(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return report(ValidationStatus.BLOCKED, ValidationCommandStatus.UNAVAILABLE)
                return report(ValidationStatus.PASSED, ValidationCommandStatus.PASSED)

        tasks = MemoryTaskRepository()
        await tasks.create(task)
        records = create_memory_workflow_repositories()
        runner = FlakyRunner()
        service = ValidationService(
            Settings(_env_file=None, environment="testing", storage="memory"),
            tasks,
            records,
            runner=runner,
        )

        result = await service.validate(task, execution, workspace)

        assert runner.calls == 2
        assert result.overall_status == ValidationStatus.PASSED
        assert result.summary.startswith("Validation was retried once")
        saved_reports = await records.validation_reports.list_for_task(task.id)
        assert len(saved_reports) == 1
        assert saved_reports[0].overall_status == ValidationStatus.PASSED

    asyncio.run(scenario())
