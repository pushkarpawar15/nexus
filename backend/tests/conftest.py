import pytest
from fastapi.testclient import TestClient

from nexus_backend.app import create_app
from nexus_backend.config import Settings
from nexus_backend.repositories import InMemoryTaskRepository


@pytest.fixture
def api():
    repository = InMemoryTaskRepository()
    settings = Settings(
        task_repository="memory",
        approved_repository_keys=frozenset({"nexus-core", "sample-service"}),
        google_cloud_project=None,
        firestore_database="(default)",
        firestore_tasks_collection="nexus_tasks",
        firestore_events_collection="nexus_task_events",
    )
    app = create_app(settings=settings, repository=repository)
    return TestClient(app), repository


@pytest.fixture
def create_task(api):
    client, _ = api

    def create(**overrides):
        body = {
            "title": "Add task search",
            "description": "Search tasks by title",
            "repository_key": "nexus-core",
            **overrides,
        }
        response = client.post("/api/tasks", json=body)
        assert response.status_code == 201, response.text
        return response.json()

    return create
