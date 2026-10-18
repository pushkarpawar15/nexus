"""Follow-up guidance on completed tasks reopens them for re-analysis."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.models.task import (
    Complexity,
    ExecutionMode,
    ModelTaskAnalysis,
    RiskLevel,
    TaskStatus,
)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="testing",
        storage="memory",
        firebase_project_id="demo-nexus",
        google_cloud_project="demo-nexus",
        approved_repositories={"sample": {"owner": "example-org", "repo": "sample"}},
        ai_permissions_raw="ai_execute,ai_validate,security_review",
    )


class FakeAnalyzer:
    async def analyze(self, *_args):
        return ModelTaskAnalysis(
            risk=RiskLevel.LOW,
            complexity=Complexity.LOW,
            mode=ExecutionMode.AI,
            assigned_agents=["coder", "validator"],
            approval_required=False,
            reason="A bounded follow-up fix.",
            steps=["Inspect the parser.", "Add a regression check."],
            validation_plan=["Run the repository's existing validation checks."],
        )


def _app() -> "object":
    app = create_app(settings=_settings())
    app.state.analysis_service.analyzer = FakeAnalyzer()
    app.state.firebase_token_verifier = lambda _token, _settings: {
        "uid": "operator-1",
        "nexus_role": "operator",
    }
    return app


def _create_and_complete(app, client, headers) -> str:
    created = client.post(
        "/api/tasks",
        headers=headers,
        json={
            "title": "Fix parser edge case",
            "description": "Handle an empty token sequence.",
            "repository_key": "sample",
            "task_type": "BUGFIX",
        },
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    analyzed = client.post(f"/api/tasks/{task_id}/analyze", headers=headers)
    assert analyzed.status_code == 200, analyzed.text

    async def complete() -> None:
        task = await app.state.task_repository.get(task_id)
        completed = task.model_copy(update={"status": TaskStatus.COMPLETED})
        await app.state.task_repository.replace(completed, expected_updated_at=task.updated_at)

    asyncio.run(complete())
    return task_id


def test_completed_task_follow_up_reopens_and_reanalyzes() -> None:
    app = _app()
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer local-token"}
        task_id = _create_and_complete(app, client, headers)

        response = client.post(
            f"/api/tasks/{task_id}/input",
            headers=headers,
            json={
                "answer": "Also handle a lone semicolon token.",
                "follow_up": "fix_request",
            },
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["status"] == "READY"
        assert payload["human_guidance"].startswith("Follow-up (fix requested):")
        assert "Also handle a lone semicolon token." in payload["human_guidance"]

        reopen = payload["transition_history"][-3]
        assert reopen["from_status"] == "COMPLETED"
        assert reopen["to_status"] == "ANALYZING"
        assert "Follow-up" in reopen["reason"]

        events = {
            event["event_type"]: event
            for event in client.get(f"/api/tasks/{task_id}/history", headers=headers).json()
        }
        assert "human.input_received" in events
        assert events["human.input_received"]["metadata"]["follow_up"] == "fix_request"


def test_completed_task_accepts_follow_up_without_intent() -> None:
    app = _app()
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer local-token"}
        task_id = _create_and_complete(app, client, headers)

        response = client.post(
            f"/api/tasks/{task_id}/input",
            headers=headers,
            json={"answer": "Please document the empty-token behavior too."},
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["status"] == "READY"
        assert payload["human_guidance"].startswith("Please document")


def test_public_transitions_cannot_reopen_a_completed_task() -> None:
    app = _app()
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer local-token"}
        task_id = _create_and_complete(app, client, headers)

        to_ready = client.post(
            f"/api/tasks/{task_id}/transitions",
            headers=headers,
            json={"to_status": "READY", "reason": "Reopen for more work."},
        )
        assert to_ready.status_code == 409

        to_analyzing = client.post(
            f"/api/tasks/{task_id}/transitions",
            headers=headers,
            json={"to_status": "ANALYZING", "reason": "Reopen for more work."},
        )
        assert to_analyzing.status_code == 422
