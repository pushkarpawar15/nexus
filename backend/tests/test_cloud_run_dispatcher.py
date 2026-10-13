from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.core.config import Settings
from app.repositories.records import create_memory_workflow_repositories
from app.schemas.execution import ExecutionRecord, ExecutionStatus
from app.services.cloud_run_dispatcher import CloudRunJobsDispatcher


def test_dispatch_accepts_job_without_waiting_for_execution_completion() -> None:
    async def scenario() -> None:
        records = create_memory_workflow_repositories()
        now = datetime.now(timezone.utc)
        execution = ExecutionRecord(
            id="execution-1",
            task_id="task-1",
            assigned_worker="coder",
            status=ExecutionStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )
        await records.executions.create(execution)

        class Response:
            status_code = 200

            def json(self):
                return {"name": "operations/operation-1"}

        class Session:
            def post(self, *_args, **_kwargs):
                return Response()

            def get(self, *_args, **_kwargs):
                raise AssertionError("dispatch must not wait for the worker to finish")

        dispatcher = CloudRunJobsDispatcher(
            Settings(_env_file=None, google_cloud_project="demo-nexus"), records
        )
        dispatcher._session = lambda: Session()

        assert await dispatcher.dispatch(execution.id) is True
        saved = await records.executions.get(execution.id)
        assert saved is not None
        assert saved.status == ExecutionStatus.QUEUED
        assert saved.cloud_execution_name is None

    asyncio.run(scenario())


def test_dispatch_persists_execution_name_when_cloud_run_returns_it() -> None:
    async def scenario() -> None:
        records = create_memory_workflow_repositories()
        now = datetime.now(timezone.utc)
        execution = ExecutionRecord(
            id="execution-2",
            task_id="task-2",
            assigned_worker="coder",
            status=ExecutionStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )
        await records.executions.create(execution)
        execution_name = (
            "projects/demo-nexus/locations/asia-south1/jobs/nexus-execution-worker/"
            "executions/run-1"
        )

        class Response:
            status_code = 200

            def json(self):
                return {
                    "name": "operations/operation-2",
                    "metadata": {"name": execution_name},
                }

        class Session:
            def post(self, *_args, **_kwargs):
                return Response()

        dispatcher = CloudRunJobsDispatcher(
            Settings(_env_file=None, google_cloud_project="demo-nexus"), records
        )
        dispatcher._session = lambda: Session()

        assert await dispatcher.dispatch(execution.id) is True
        saved = await records.executions.get(execution.id)
        assert saved is not None
        assert saved.cloud_execution_name == execution_name

    asyncio.run(scenario())
