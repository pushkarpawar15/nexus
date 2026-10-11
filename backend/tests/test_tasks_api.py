from datetime import datetime


def test_task_crud_and_pagination(api, create_task):
    client, _ = api
    first = create_task(title="First task")
    create_task(title="Second task", repository_key="sample-service")

    assert first["status"] == "BACKLOG"
    assert first["approval_required"] is True
    assert first["risk"] == "UNASSESSED"
    assert first["execution_mode"] == "AI"
    assert first["id"]
    assert first["created_at"]
    assert first["updated_at"]

    fetched = client.get(f"/api/tasks/{first['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "First task"

    updated = client.patch(
        f"/api/tasks/{first['id']}",
        json={"title": "Renamed task", "description": "Updated details"},
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "Renamed task"
    assert updated.json()["description"] == "Updated details"
    assert datetime.fromisoformat(updated.json()["updated_at"].replace("Z", "+00:00")) >= (
        datetime.fromisoformat(first["updated_at"].replace("Z", "+00:00"))
    )

    page_one = client.get("/api/tasks", params={"limit": 1, "offset": 0})
    page_two = client.get("/api/tasks", params={"limit": 1, "offset": 1})
    assert page_one.status_code == page_two.status_code == 200
    assert len(page_one.json()["items"]) == 1
    assert page_one.json()["has_more"] is True
    assert page_two.json()["has_more"] is False
    assert page_one.json()["items"][0]["id"] != page_two.json()["items"][0]["id"]

    status_filter = client.get("/api/tasks", params={"status": "BACKLOG"})
    assert len(status_filter.json()["items"]) == 2
    assert all(item["status"] == "BACKLOG" for item in status_filter.json()["items"])


def test_tasks_persist_in_the_selected_repository(api, create_task):
    client, repository = api
    created = create_task(title="Persist me")

    # A newly constructed API service over the same adapter sees the stored task.
    from fastapi.testclient import TestClient

    from nexus_backend.app import create_app
    from nexus_backend.config import Settings

    settings = Settings(
        task_repository="memory",
        approved_repository_keys=frozenset({"nexus-core"}),
        google_cloud_project=None,
        firestore_database="(default)",
        firestore_tasks_collection="nexus_tasks",
        firestore_events_collection="nexus_task_events",
    )
    second_client = TestClient(create_app(settings=settings, repository=repository))
    assert second_client.get(f"/api/tasks/{created['id']}").json()["title"] == "Persist me"
    assert client.get(f"/api/tasks/{created['id']}").status_code == 200


def test_repository_access_is_allowlisted(api):
    client, _ = api
    response = client.post(
        "/api/tasks",
        json={"title": "No", "repository_key": "unapproved/repo"},
    )
    assert response.status_code == 422


def test_clients_cannot_set_workflow_state_or_approval(api):
    client, _ = api
    create_response = client.post(
        "/api/tasks",
        json={
            "title": "No state override",
            "repository_key": "nexus-core",
            "status": "APPROVED",
            "approval_required": False,
        },
    )
    assert create_response.status_code == 422

    task = client.post(
        "/api/tasks",
        json={"title": "Normal task", "repository_key": "nexus-core"},
    ).json()
    patch_response = client.patch(
        f"/api/tasks/{task['id']}",
        json={"status": "APPROVED"},
    )
    assert patch_response.status_code == 422


def test_patch_rejects_empty_or_null_updates(api, create_task):
    client, _ = api
    task = create_task()
    assert client.patch(f"/api/tasks/{task['id']}", json={}).status_code == 422
    assert client.patch(f"/api/tasks/{task['id']}", json={"title": None}).status_code == 422


def test_get_missing_task_returns_404(api):
    client, _ = api
    assert client.get("/api/tasks/missing").status_code == 404
