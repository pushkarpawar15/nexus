from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError

from app.core.auth import UserIdentity, UserRole, get_current_user
from app.core.config import Settings
from app.core.errors import NexusError


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


def test_default_role_applies_when_the_claim_is_missing() -> None:
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


def test_production_default_role_grants_access() -> None:
    settings = _settings(
        environment="production",
        storage="firestore",
        nexus_default_role="admin",
    )
    assert _identity({"uid": "new-user"}, settings).role is UserRole.ADMIN


def test_production_rejects_users_without_claim_or_default() -> None:
    settings = _settings(
        environment="production",
        storage="firestore",
        nexus_default_role=None,
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


def test_default_role_gates_api_endpoints() -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = _settings(
        firebase_project_id="demo-nexus",
        cors_origins_raw="http://localhost:5173",
        approved_repositories={"sample": {"owner": "example-org", "repo": "sample"}},
        nexus_default_role="viewer",
    )
    app = create_app(settings=settings)
    app.state.firebase_token_verifier = lambda _token, _settings: {"uid": "new-user"}
    client = TestClient(app)
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
