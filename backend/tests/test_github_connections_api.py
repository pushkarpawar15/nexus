import asyncio

from fastapi.testclient import TestClient

from app.core.config import RepositoryTarget, Settings
from app.main import create_app


def _client() -> TestClient:
    settings = Settings(
        _env_file=None,
        environment="testing",
        storage="memory",
        firebase_project_id="demo-nexus",
    )
    app = create_app(settings=settings)
    app.state.firebase_token_verifier = lambda token, _settings: {"uid": token}

    async def validate_pat(_token: str, owner: str, repo: str):
        target = RepositoryTarget(
            owner=owner,
            repo=repo,
            github_repository_id=481516,
            label=f"{owner}/{repo}",
            base_branch="main",
        )
        return target, "octocat"

    app.state.github_pat.connect = validate_pat
    return TestClient(app)


def _headers(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user_id}"}


def _connect(client: TestClient, user_id: str) -> dict:
    response = client.post(
        "/api/github/pat",
        headers=_headers(user_id),
        json={
            "token": "test-token-that-is-not-real-123456789",
            "owner": "example",
            "repo": "project",
        },
    )
    assert response.status_code == 200
    assert "test-token" not in response.text
    assert "secret_name" not in response.text
    return response.json()


def test_repository_connections_are_user_scoped_and_tasks_return_after_reconnect() -> None:
    client = _client()
    connected = _connect(client, "user-a")
    repository_key = connected["repository_info"]["key"]
    created = client.post(
        "/api/tasks",
        headers=_headers("user-a"),
        json={
            "title": "Keep task history",
            "description": "The task remains after disconnect.",
            "repository_key": repository_key,
        },
    )
    assert created.status_code == 201
    task_id = created.json()["id"]

    assert client.get("/api/tasks", headers=_headers("user-a")).json()["total"] == 1
    assert client.get("/api/tasks", headers=_headers("user-b")).json()["total"] == 0
    assert client.get(f"/api/tasks/{task_id}", headers=_headers("user-b")).status_code == 404

    impact = client.get(
        f"/api/repositories/{repository_key}/removal-impact",
        headers=_headers("user-a"),
    )
    assert impact.status_code == 200
    assert impact.json()["pending_task_count"] == 1

    removed_without_confirmation = client.request(
        "DELETE",
        f"/api/repositories/{repository_key}",
        headers=_headers("user-a"),
        json={"confirm_pending_tasks": False},
    )
    assert removed_without_confirmation.status_code == 409
    removed = client.request(
        "DELETE",
        f"/api/repositories/{repository_key}",
        headers=_headers("user-a"),
        json={"confirm_pending_tasks": True},
    )
    assert removed.status_code == 204
    assert client.get("/api/tasks", headers=_headers("user-a")).json()["total"] == 0
    assert asyncio.run(client.app.state.task_repository.get(task_id))

    _connect(client, "user-b")
    assert client.get("/api/tasks", headers=_headers("user-b")).json()["total"] == 1
    reconnected = _connect(client, "user-a")
    assert reconnected["repository_info"]["key"] == repository_key
    assert client.get("/api/tasks", headers=_headers("user-a")).json()["total"] == 1
