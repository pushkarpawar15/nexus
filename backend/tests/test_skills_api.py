from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_skill_endpoints_enforce_scope_and_role_permissions() -> None:
    settings = Settings(
        _env_file=None,
        environment="testing",
        storage="memory",
        firebase_project_id="demo-nexus",
        approved_repositories={
            "sample": {"owner": "example-org", "repo": "sample"},
            "other": {"owner": "example-org", "repo": "other"},
        },
    )
    app = create_app(settings=settings)
    app.state.firebase_token_verifier = lambda token, _settings: {
        "uid": token,
        "nexus_role": "admin" if token == "admin" else "operator",
    }
    client = TestClient(app)
    operator = {"Authorization": "Bearer operator"}
    admin = {"Authorization": "Bearer admin"}

    global_skill = {
        "name": "Global conventions",
        "description": "Shared guidance",
        "instructions": "Prefer small, focused changes.",
        "scope": "global",
        "repository_key": None,
        "enabled": True,
    }
    assert client.post("/api/skills", headers=operator, json=global_skill).status_code == 403

    repository_skill = {
        **global_skill,
        "name": "Repository conventions",
        "scope": "repository",
        "repository_key": "sample",
    }
    created = client.post("/api/skills", headers=operator, json=repository_skill)
    assert created.status_code == 201
    assert created.json()["version"] == 1
    skill_id = created.json()["id"]

    global_created = client.post("/api/skills", headers=admin, json=global_skill)
    assert global_created.status_code == 201
    assert len(client.get("/api/skills", headers=operator).json()["items"]) == 1
    assert len(
        client.get(
            "/api/skills",
            headers=operator,
            params={"repository_key": "sample"},
        ).json()["items"]
    ) == 2
    assert (
        client.get(
            "/api/skills",
            headers=operator,
            params={"repository_key": "unapproved"},
        ).status_code
        == 422
    )

    updated = client.patch(
        f"/api/skills/{skill_id}",
        headers=operator,
        json={**repository_skill, "enabled": False},
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert updated.json()["enabled"] is False
    assert client.delete(f"/api/skills/{skill_id}", headers=operator).status_code == 204
    global_skill_id = global_created.json()["id"]
    assert client.delete(f"/api/skills/{global_skill_id}", headers=operator).status_code == 403


def test_skill_api_rejects_blank_and_oversized_instruction_content() -> None:
    settings = Settings(
        _env_file=None,
        environment="testing",
        storage="memory",
        firebase_project_id="demo-nexus",
        approved_repositories={"sample": {"owner": "example-org", "repo": "sample"}},
    )
    app = create_app(settings=settings)
    app.state.firebase_token_verifier = lambda _token, _settings: {"uid": "operator"}
    client = TestClient(app)
    headers = {"Authorization": "Bearer operator"}
    body = {
        "name": "Bounded skill",
        "description": "Validate user content",
        "instructions": "Useful directions.",
        "scope": "repository",
        "repository_key": "sample",
        "enabled": True,
    }

    assert (
        client.post(
            "/api/skills",
            headers=headers,
            json={**body, "instructions": "   "},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/skills",
            headers=headers,
            json={**body, "instructions": "x" * 20_001},
        ).status_code
        == 422
    )
