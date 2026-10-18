from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.auth import UserIdentity, UserRole, get_current_user
from app.core.config import Settings
from app.core.errors import NexusError
from app.main import create_app


def _settings(**overrides) -> Settings:
    values: dict = {
        "_env_file": None,
        "environment": "testing",
        "storage": "memory",
        "firebase_project_id": "demo-nexus",
    }
    values.update(overrides)
    return Settings(**values)


def _identity(claims: dict, settings: Settings) -> UserIdentity:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                firebase_token_verifier=lambda _token, _settings: claims,
            )
        )
    )
    credentials = HTTPAuthorizationCredentials(scheme="bearer", credentials="token")
    return asyncio.run(get_current_user(request, credentials, settings))


def _client(settings: Settings, claims: dict) -> TestClient:
    app = create_app(settings=settings)
    app.state.firebase_token_verifier = lambda _token, _settings: claims
    return TestClient(app)


def test_unset_default_role_is_admin_so_new_users_get_every_feature() -> None:
    settings = _settings()

    assert settings.nexus_default_role == "admin"
    assert _identity({"uid": "new-user"}, settings).role is UserRole.ADMIN


def test_configured_default_role_replaces_the_built_in_default() -> None:
    settings = _settings(nexus_default_role="viewer")
    assert _identity({"uid": "new-user"}, settings).role is UserRole.VIEWER


def test_role_claim_wins_over_the_default_role() -> None:
    settings = _settings(nexus_default_role="viewer")
    identity = _identity({"uid": "u1", "nexus_role": "admin"}, settings)
    assert identity.role is UserRole.ADMIN


def test_unrecognized_claim_falls_back_to_the_default_role() -> None:
    settings = _settings(nexus_default_role="operator")
    identity = _identity({"uid": "u1", "nexus_role": "owner"}, settings)
    assert identity.role is UserRole.OPERATOR


def test_production_grants_the_default_admin_role() -> None:
    settings = _settings(environment="production", storage="firestore")
    assert _identity({"uid": "new-user"}, settings).role is UserRole.ADMIN


def test_production_rejects_users_when_the_default_role_is_disabled() -> None:
    settings = _settings(
        environment="production",
        storage="firestore",
        nexus_default_role="",
    )
    with pytest.raises(NexusError) as excinfo:
        _identity({"uid": "new-user"}, settings)
    assert excinfo.value.status_code == 403
    assert excinfo.value.message == "The account has no recognized NEXUS role."


def test_invalid_default_role_is_rejected_by_settings() -> None:
    with pytest.raises(ValidationError):
        _settings(nexus_default_role="owner")


def test_blank_default_role_is_normalized_to_no_default() -> None:
    assert _settings(nexus_default_role="  ").nexus_default_role is None


def test_me_endpoint_reports_the_default_admin_role() -> None:
    client = _client(_settings(), {"uid": "new-user", "email": "new@example.com"})

    response = client.get("/api/me", headers={"Authorization": "Bearer token"})

    assert response.status_code == 200
    assert response.json() == {
        "uid": "new-user",
        "email": "new@example.com",
        "role": "admin",
    }


def test_me_endpoint_prefers_the_role_claim() -> None:
    client = _client(
        _settings(nexus_default_role="viewer"),
        {"uid": "admin-user", "nexus_role": "admin"},
    )

    response = client.get("/api/me", headers={"Authorization": "Bearer token"})

    assert response.status_code == 200
    assert response.json()["role"] == "admin"


def test_default_role_reaches_admin_only_endpoints() -> None:
    client = _client(_settings(), {"uid": "new-user"})

    response = client.post(
        "/api/tasks/missing-task/executions/missing-execution/approval",
        headers={"Authorization": "Bearer token"},
        json={"status": "APPROVED", "reason": "Ship it"},
    )

    # Authorization passed; the unknown task is the only failure.
    assert response.status_code == 404


def test_viewer_default_role_is_blocked_from_admin_only_endpoints() -> None:
    client = _client(_settings(nexus_default_role="viewer"), {"uid": "new-user"})

    response = client.post(
        "/api/tasks/missing-task/executions/missing-execution/approval",
        headers={"Authorization": "Bearer token"},
        json={"status": "APPROVED", "reason": "Ship it"},
    )

    assert response.status_code == 403


def test_viewer_default_role_still_gates_operator_endpoints() -> None:
    client = _client(
        _settings(
            cors_origins_raw="http://localhost:5173",
            approved_repositories={"sample": {"owner": "example-org", "repo": "sample"}},
            nexus_default_role="viewer",
        ),
        {"uid": "new-user"},
    )
    headers = {"Authorization": "Bearer token"}

    # Viewers may read repositories but may not create tasks.
    assert client.get("/api/repositories", headers=headers).status_code == 200
    assert (
        client.post(
            "/api/tasks",
            headers=headers,
            json={
                "title": "New user task",
                "description": "Created after first sign-in",
                "repository_key": "sample",
            },
        ).status_code
        == 403
    )
