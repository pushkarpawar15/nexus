"""Task deletion is allowed in every status except IN_PROGRESS and VALIDATING."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.models.task import TaskStatus

BLOCKED_STATUSES = (TaskStatus.IN_PROGRESS, TaskStatus.VALIDATING)
DELETABLE_STATUSES = tuple(status for status in TaskStatus if status not in BLOCKED_STATUSES)


def _app() -> object:
    app = create_app(
        settings=Settings(
            _env_file=None,
            environment="testing",
            storage="memory",
            firebase_project_id="demo-nexus",
            google_cloud_project="demo-nexus",
            approved_repositories={"sample": {"owner": "example-org", "repo": "sample"}},
            ai_permissions_raw="ai_execute,ai_validate,security_review",
        )
    )
    app.state.firebase_token_verifier = lambda _token, _settings: {
        "uid": "operator-1",
        "nexus_role": "operator",
    }
    return app


def _create_task(app, client, headers) -> str:
    created = client.post(
        "/api/tasks",
        headers=headers,
        json={
            "title": "Deletable task",
            "description": "Covers every task status.",
            "repository_key": "sample",
            "task_type": "OTHER",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _stage(app, task_id: str, status: TaskStatus) -> None:
    async def run() -> None:
        task = await app.state.task_repository.get(task_id)
        assert task is not None
        staged = task.model_copy(update={"status": status})
        await app.state.task_repository.replace(staged, expected_updated_at=task.updated_at)

    asyncio.run(run())


@pytest.mark.parametrize("status", DELETABLE_STATUSES, ids=lambda value: value.value)
def test_task_can_be_deleted_in_status(status: TaskStatus) -> None:
    app = _app()
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer local-token"}
        task_id = _create_task(app, client, headers)
        _stage(app, task_id, status)

        deleted = client.delete(f"/api/tasks/{task_id}", headers=headers)
        assert deleted.status_code == 204, deleted.text
        assert client.get(f"/api/tasks/{task_id}", headers=headers).status_code == 404


@pytest.mark.parametrize("status", BLOCKED_STATUSES, ids=lambda value: value.value)
def test_task_cannot_be_deleted_while_running_or_validating(status: TaskStatus) -> None:
    app = _app()
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer local-token"}
        task_id = _create_task(app, client, headers)
        _stage(app, task_id, status)

        rejected = client.delete(f"/api/tasks/{task_id}", headers=headers)
        assert rejected.status_code == 409, rejected.text
        assert "in progress or validating" in rejected.text
        assert client.get(f"/api/tasks/{task_id}", headers=headers).status_code == 200


def test_stale_delete_conflicts_instead_of_faking_success() -> None:
    app = _app()
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer local-token"}
        task_id = _create_task(app, client, headers)

        async def delete_twice() -> tuple[int, int]:
            task = await app.state.task_repository.get(task_id)
            assert task is not None
            first = await app.state.task_repository.delete(
                task.id, expected_updated_at=task.updated_at
            )
            second = await app.state.task_repository.delete(
                task.id, expected_updated_at=task.updated_at
            )
            return int(first), int(second)

        assert asyncio.run(delete_twice()) == (1, 0)
