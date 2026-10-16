from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def _client() -> TestClient:
    settings = Settings(
        _env_file=None,
        environment="testing",
        storage="memory",
        firebase_project_id="demo-nexus",
        google_cloud_project="demo-nexus",
        approved_repositories={"sample": {"owner": "example-org", "repo": "sample"}},
    )
    app = create_app(settings=settings)
    app.state.firebase_token_verifier = lambda _token, _settings: {
        "uid": "operator-1",
        "nexus_role": "operator",
    }
    return TestClient(app)


def _create_task(client: TestClient) -> dict:
    response = client.post(
        "/api/tasks",
        headers={"Authorization": "Bearer test-token"},
        json={
            "title": "Add task metadata",
            "description": "Track ownership and delivery planning.",
            "repository_key": "sample",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_task_metadata_can_be_created_updated_and_cleared() -> None:
    with _client() as client:
        created = client.post(
            "/api/tasks",
            headers={"Authorization": "Bearer test-token"},
            json={
                "title": "Add task metadata",
                "description": "Track ownership and delivery planning.",
                "repository_key": "sample",
                "assignee": "  developer@example.com ",
                "priority": "HIGH",
                "due_date": "2026-07-15",
                "labels": [" frontend ", "bug"],
            },
        )
        assert created.status_code == 201, created.text
        task = created.json()
        assert task["assignee"] == "developer@example.com"
        assert task["priority"] == "HIGH"
        assert task["due_date"] == "2026-07-15"
        assert task["labels"] == ["frontend", "bug"]

        response = client.patch(
            f"/api/tasks/{task['id']}",
            headers={"Authorization": "Bearer test-token"},
            json={"assignee": None, "priority": "URGENT", "labels": ["release"]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["assignee"] is None
        assert response.json()["priority"] == "URGENT"
        assert response.json()["labels"] == ["release"]


def test_task_metadata_remains_editable_after_work_starts() -> None:
    with _client() as client:
        task = _create_task(client)
        headers = {"Authorization": "Bearer test-token"}
        transition = client.post(
            f"/api/tasks/{task['id']}/transitions",
            headers=headers,
            json={"to_status": "BLOCKED", "reason": "Waiting on a dependency"},
        )
        assert transition.status_code == 200, transition.text

        update = client.patch(
            f"/api/tasks/{task['id']}",
            headers=headers,
            json={"assignee": "owner@example.com", "priority": "URGENT"},
        )
        assert update.status_code == 200, update.text
        assert update.json()["assignee"] == "owner@example.com"
        assert update.json()["priority"] == "URGENT"
        assert (
            client.patch(
                f"/api/tasks/{task['id']}",
                headers=headers,
                json={"title": "Renamed"},
            ).status_code
            == 409
        )


def test_task_metadata_rejects_invalid_values() -> None:
    with _client() as client:
        task = _create_task(client)
        headers = {"Authorization": "Bearer test-token"}
        assert (
            client.patch(
                f"/api/tasks/{task['id']}",
                headers=headers,
                json={"priority": "P0"},
            ).status_code
            == 422
        )
        assert (
            client.patch(
                f"/api/tasks/{task['id']}",
                headers=headers,
                json={"labels": [""]},
            ).status_code
            == 422
        )
