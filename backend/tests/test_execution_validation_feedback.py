from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

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
from app.services.execution_pipeline import ExecutionPipeline


def test_validation_diagnostics_are_passed_to_the_coding_repair(tmp_path: Path) -> None:
    async def scenario() -> None:
        now = datetime.now(timezone.utc)
        task = Task(
            id="task-validation-feedback",
            title="Fix an implementation issue",
            description="Make the repository checks pass",
            repository_key="github-123",
            status=TaskStatus.VALIDATING,
            created_at=now,
            updated_at=now,
            created_by="operator-1",
        )
        workspace_path = tmp_path / "workspace"
        workspace_path.mkdir(parents=True, exist_ok=True)
        target_file = workspace_path / "README.md"
        target_file.write_text("before repair\n", encoding="utf-8")
        fingerprint = compute_patch_fingerprint(workspace_path)
        execution = ExecutionRecord(
            id="execution-validation-feedback",
            task_id=task.id,
            assigned_worker="coding-agent",
            status=ExecutionStatus.RUNNING,
            created_at=now,
            updated_at=now,
            repository_key=task.repository_key,
            patch_fingerprint=fingerprint,
            changed_files=["README.md"],
        )
        tasks = MemoryTaskRepository()
        await tasks.create(task)
        records = create_memory_workflow_repositories()
        await records.executions.create(execution)

        class RepairCoder:
            received_feedback = None

            async def edit(self, _task, workspace, **options):
                self.received_feedback = options.get("previous_error")
                target_file.write_text("after repair\n", encoding="utf-8")
                return (
                    ["README.md"],
                    compute_patch_fingerprint(workspace.path),
                    "Updated README.md.",
                )

        class RecoveringValidator:
            calls = 0

            async def validate(self, current_task, current_execution, _workspace):
                self.calls += 1
                passed = self.calls > 1
                status = (
                    ValidationCommandStatus.PASSED if passed else ValidationCommandStatus.FAILED
                )
                command = ValidationCommandResult(
                    command_id="pytest",
                    command="pytest -q",
                    status=status,
                    exit_code=0 if passed else 1,
                    stderr_excerpt="" if passed else "AssertionError: expected IDR, got USD",
                    error=None if passed else "Two currency assertions failed.",
                )
                return ValidationReport(
                    id=f"validation-feedback-{self.calls}",
                    execution_id=current_execution.id,
                    task_id=current_task.id,
                    patch_fingerprint=current_execution.patch_fingerprint,
                    overall_status=ValidationStatus.PASSED if passed else ValidationStatus.FAILED,
                    started_at=now,
                    finished_at=now,
                    commands=[command],
                    summary="Checks passed." if passed else "Tests found a currency regression.",
                )

        coder = RepairCoder()
        validator = RecoveringValidator()
        pipeline = ExecutionPipeline(
            Settings(_env_file=None, environment="testing", storage="memory"),
            tasks,
            records,
            coder=coder,
            validator=validator,
        )
        workspace = SimpleNamespace(
            path=workspace_path,
            branch="test-branch",
            base_commit_sha="a" * 40,
        )

        result = await pipeline._validate_until_pass(task, execution, workspace, {})

        assert result is not None
        assert result[1].overall_status == ValidationStatus.PASSED
        assert validator.calls == 2
        assert coder.received_feedback is not None
        assert "AssertionError: expected IDR, got USD" in coder.received_feedback
        assert "pytest -q" in coder.received_feedback

    asyncio.run(scenario())
