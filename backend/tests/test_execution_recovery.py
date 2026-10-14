from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.core.config import Settings
from app.models.task import (
    Complexity,
    ExecutionMode,
    ExecutionPlan,
    PlanSource,
    RiskLevel,
    Task,
    TaskStatus,
    TaskType,
)
from app.repositories.records import create_memory_workflow_repositories
from app.repositories.tasks import MemoryTaskRepository
from app.schemas.execution import ExecutionRecord, ExecutionStatus
from app.services.execution_service import ExecutionService


def _task(status: TaskStatus) -> Task:
    now = datetime.now(timezone.utc)
    plan = ExecutionPlan(
        risk=RiskLevel.LOW,
        complexity=Complexity.LOW,
        mode=ExecutionMode.AI,
        assigned_agents=["coder", "validator"],
        approval_required=False,
        reason="A bounded test task.",
        steps=["Update the UI."],
        validation_plan=["Run existing repository checks."],
        source=PlanSource.GEMINI,
        model_id="test-model",
        policy_reason="Low-risk test task.",
        security_review_required=False,
    )
    return Task(
        id="task-recovery",
        title="Recover cancelled execution",
        description="Resume after an interrupted worker.",
        repository_key="example/repository",
        task_type=TaskType.BUGFIX,
        status=status,
        risk=RiskLevel.LOW,
        complexity=Complexity.LOW,
        mode=ExecutionMode.AI,
        assigned_agents=["coder", "validator"],
        execution_plan=plan,
        approval_required=False,
        created_at=now,
        updated_at=now,
        created_by="operator",
    )


def _cancelled_execution(task_id: str) -> ExecutionRecord:
    now = datetime.now(timezone.utc)
    return ExecutionRecord(
        id="execution-cancelled",
        task_id=task_id,
        assigned_worker="coder",
        status=ExecutionStatus.CANCELLED,
        created_at=now,
        updated_at=now,
        repository_key="example/repository",
    )


def test_stop_blocks_task_when_worker_cancellation_wins_race() -> None:
    async def scenario() -> None:
        settings = Settings(_env_file=None, environment="testing", storage="memory")
        tasks = MemoryTaskRepository()
        records = create_memory_workflow_repositories()
        task = _task(TaskStatus.VALIDATING)
        execution = _cancelled_execution(task.id).model_copy(
            update={"status": ExecutionStatus.RUNNING}
        )
        await tasks.create(task)
        await records.executions.create(execution)

        class RacingDispatcher:
            async def cancel(self, _execution_id: str) -> None:
                current = await records.executions.get(execution.id)
                assert current is not None
                await records.executions.replace(
                    current.model_copy(
                        update={
                            "status": ExecutionStatus.CANCELLED,
                            "finished_at": datetime.now(timezone.utc),
                            "final_result": "CANCELLED",
                        }
                    ),
                    expected_updated_at=current.updated_at,
                )

        service = ExecutionService(settings, tasks, records, RacingDispatcher())
        await service.stop(task, execution.id, "operator")

        saved_task = await tasks.get(task.id)
        assert saved_task is not None
        assert saved_task.status == TaskStatus.BLOCKED

    asyncio.run(scenario())


def test_retry_recovers_task_with_cancelled_latest_execution() -> None:
    async def scenario() -> None:
        settings = Settings(
            _env_file=None,
            environment="testing",
            storage="memory",
            coding_enabled=True,
            validation_enabled=True,
            ai_permissions_raw="ai_execute,ai_validate,security_review",
        )
        tasks = MemoryTaskRepository()
        records = create_memory_workflow_repositories()
        task = _task(TaskStatus.VALIDATING)
        await tasks.create(task)
        await records.executions.create(_cancelled_execution(task.id))

        class Dispatcher:
            async def dispatch(self, _execution_id: str) -> bool:
                return True

        service = ExecutionService(settings, tasks, records, Dispatcher())
        execution = await service.retry(task, "operator")

        saved_task = await tasks.get(task.id)
        assert saved_task is not None
        assert saved_task.status == TaskStatus.IN_PROGRESS
        assert execution.status == ExecutionStatus.QUEUED
        assert len(await records.executions.list_for_task(task.id)) == 2

    asyncio.run(scenario())
