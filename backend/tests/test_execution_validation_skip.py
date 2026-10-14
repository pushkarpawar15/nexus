import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.core.config import Settings
from app.models.task import Task, TaskStatus
from app.repositories.records import create_memory_workflow_repositories
from app.repositories.tasks import MemoryTaskRepository
from app.schemas.execution import ExecutionRecord, ExecutionStatus
from app.schemas.validation import ValidationReport, ValidationStatus
from app.services.execution_pipeline import ExecutionPipeline


def test_skipped_validation_does_not_request_operator_input_and_blocks_execution() -> None:
    async def scenario() -> None:
        now = datetime.now(timezone.utc)
        task = Task(
            id="task-1",
            title="A validation task",
            description="Run configured checks",
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
            patch_fingerprint="a" * 64,
        )
        report = ValidationReport(
            id="validation-1",
            execution_id=execution.id,
            task_id=task.id,
            patch_fingerprint=execution.patch_fingerprint,
            overall_status=ValidationStatus.SKIPPED,
            started_at=now,
            finished_at=now,
            commands=[],
            summary=(
                "Validation skipped: No validation profile is configured for this repository."
            ),
        )

        class SkippedValidator:
            async def validate(self, *_args):
                return report

        tasks = MemoryTaskRepository()
        await tasks.create(task)
        records = create_memory_workflow_repositories()
        await records.executions.create(execution)
        pipeline = ExecutionPipeline(
            Settings(_env_file=None, environment="testing", storage="memory"),
            tasks,
            records,
            validator=SkippedValidator(),
        )

        validated = await pipeline._validate_until_pass(
            task, execution, SimpleNamespace(path=Path()), {}
        )

        assert validated is not None
        assert validated[1].overall_status == ValidationStatus.SKIPPED
        assert pipeline._input_waiters == {}
        assert await pipeline._finish_skipped_validation(task, execution, report)

        final_task = await tasks.get(task.id)
        final_execution = await records.executions.get(execution.id)
        assert final_task is not None and final_task.status == TaskStatus.BLOCKED
        assert final_execution is not None
        assert final_execution.status == ExecutionStatus.BLOCKED
        assert final_execution.final_result == "VALIDATION_SKIPPED"

    asyncio.run(scenario())
