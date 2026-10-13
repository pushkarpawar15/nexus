import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import ConfigurationError, Settings, validate_settings
from app.main import create_app
from app.repositories.records import create_memory_workflow_repositories
from app.schemas.approval import ApprovalRecord, ApprovalStatus


def test_memory_mode_health_and_readiness() -> None:
    settings = Settings(_env_file=None, environment="testing", storage="memory")
    client = TestClient(create_app(settings=settings))

    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/ready").json() == {"status": "ready"}


def test_task_api_requires_firebase_identity_and_operator_role() -> None:
    settings = Settings(
        _env_file=None,
        environment="testing",
        storage="memory",
        firebase_project_id="demo-nexus",
        cors_origins_raw="http://localhost:5173",
        approved_repositories={"sample": {"owner": "example-org", "repo": "sample"}},
    )
    app = create_app(settings=settings)
    app.state.firebase_token_verifier = lambda _token, _settings: {"uid": "local-user"}
    client = TestClient(app)

    assert client.get("/api/tasks").status_code == 401
    assert (
        client.post(
            "/api/tasks",
            json={
                "title": "Test task",
                "description": "Created through the authenticated API",
                "repository_key": "sample",
            },
        ).status_code
        == 401
    )
    created = client.post(
        "/api/tasks",
        headers={"Authorization": "Bearer local-token"},
        json={
            "title": "Test task",
            "description": "Created through the authenticated API",
            "repository_key": "sample",
        },
    )

    assert created.status_code == 201
    assert created.json()["created_by"] == "local-user"
    listed = client.get("/api/tasks", headers={"Authorization": "Bearer local-token"})
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    preflight = client.options(
        "/api/tasks",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_firestore_failure_is_visible_as_unready() -> None:
    class FailedQuery:
        async def get(self):
            raise OSError("Firestore is unavailable")

    class Collection:
        def limit(self, _: int) -> FailedQuery:
            return FailedQuery()

    class Client:
        def collection(self, _: str) -> Collection:
            return Collection()

    settings = Settings(
        _env_file=None,
        environment="development",
        storage="firestore",
        google_cloud_project="demo-nexus",
    )
    response = TestClient(create_app(settings=settings, firestore_client=Client())).get(
        "/api/ready"
    )

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "store": "firestore"}


def test_production_rejects_memory_and_emulator_hosts() -> None:
    memory = Settings(_env_file=None, environment="production", storage="memory")
    try:
        validate_settings(memory)
    except ConfigurationError:
        pass
    else:
        raise AssertionError("production memory storage should be rejected")

    emulator = Settings(
        _env_file=None,
        environment="production",
        storage="firestore",
        google_cloud_project="demo-nexus",
        firebase_project_id="demo-nexus",
        firestore_emulator_host="127.0.0.1:8080",
        approved_repositories={"sample": {"owner": "example-org", "repo": "sample"}},
    )
    try:
        validate_settings(emulator)
    except ConfigurationError:
        pass
    else:
        raise AssertionError("production emulator hosts should be rejected")


def test_storage_is_required_and_empty_optional_environment_values_are_ignored(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NEXUS_STORAGE", "")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "")
    settings = Settings(_env_file=None, environment="development")
    try:
        validate_settings(settings)
    except ConfigurationError as exc:
        assert "NEXUS_STORAGE" in str(exc)
    else:
        raise AssertionError("storage must be explicitly configured")


def test_production_execution_requires_cloud_run_backend() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        storage="firestore",
        google_cloud_project="demo-nexus",
        firebase_project_id="demo-nexus",
        approved_repositories={"sample": {"owner": "example-org", "repo": "sample"}},
        coding_enabled=True,
        validation_enabled=True,
    )
    try:
        validate_settings(settings)
    except ConfigurationError as exc:
        assert "NEXUS_EXECUTION_BACKEND=cloud_run_jobs" in str(exc)
    else:
        raise AssertionError("production execution must use the Cloud Run backend")


def test_firestore_app_factory_wires_the_workflow_collections() -> None:
    class Client:
        def __init__(self) -> None:
            self.collection_names: list[str] = []

        def collection(self, name: str):
            self.collection_names.append(name)
            return object()

    client = Client()
    settings = Settings(
        _env_file=None,
        environment="development",
        storage="firestore",
        google_cloud_project="demo-nexus",
    )

    create_app(settings=settings, firestore_client=client)

    assert set(client.collection_names) == {
        "tasks",
        "executions",
        "activity_events",
        "validation_reports",
        "security_reviews",
        "approvals",
        "pull_requests",
    }


def test_memory_approval_records_are_append_only() -> None:
    async def exercise_repository() -> None:
        approvals = create_memory_workflow_repositories().approvals
        now = datetime.now(timezone.utc)
        record = ApprovalRecord(
            id="exec-1",
            task_id="task-1",
            execution_id="exec-1",
            patch_fingerprint="a" * 64,
            status=ApprovalStatus.APPROVED,
            approver_id="admin-1",
            decided_at=now,
            reason="Reviewed patch",
        )
        await approvals.create(record)
        assert await approvals.get(record.id) == record
        replay = await approvals.create_or_get(
            record.model_copy(update={"reason": "different replay"})
        )
        assert replay == record
        with pytest.raises(TypeError, match="immutable"):
            await approvals.replace(record)

    asyncio.run(exercise_repository())


def test_execution_workspace_must_be_absolute_and_outside_checkouts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        Settings(
            _env_file=None,
            environment="development",
            storage="firestore",
            google_cloud_project="demo-nexus",
            workspace_root=Path("relative/workspaces"),
        )

    target = tmp_path / "target"
    settings = Settings(
        _env_file=None,
        environment="development",
        storage="firestore",
        google_cloud_project="demo-nexus",
        coding_enabled=True,
        validation_enabled=True,
        workspace_root=target / "workspaces",
        approved_repositories={
            "nexus_pay": {
                "owner": "example-org",
                "repo": "payflow",
                "source_path": target,
            }
        },
    )

    with pytest.raises(ConfigurationError, match="separate"):
        validate_settings(settings)


def test_coding_model_timeout_defaults_to_three_minutes() -> None:
    settings = Settings(_env_file=None, environment="testing", storage="memory")

    assert settings.coding_model_timeout_seconds == 180
