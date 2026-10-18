"""Follow-up guidance at PR_CREATED reopens the task for another patch on the same PR."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.core.config import RepositoryTarget, Settings
from app.integrations.github import GitHubClient, GitHubOperationError
from app.main import create_app
from app.models.task import (
    Complexity,
    ExecutionMode,
    ModelTaskAnalysis,
    RiskLevel,
    Task,
    TaskStatus,
    TaskType,
)
from app.repositories.records import create_memory_workflow_repositories
from app.repositories.tasks import MemoryTaskRepository
from app.schemas.pull_request import PullRequestRecord, PullRequestStatus
from app.services.coding_workspace import CodingWorkspaceError
from app.services.github_pr_service import GitHubPRService, _lease_push_args

PR_URL = "https://github.com/example-org/sample/pull/7"
BRANCH = "nexus/task-demo"


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
            reason="A bounded follow-up patch.",
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


def _create_and_publish(app, client, headers, status: TaskStatus = TaskStatus.PR_CREATED) -> str:
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

    async def stage() -> None:
        task = await app.state.task_repository.get(task_id)
        staged = task.model_copy(update={"status": status})
        await app.state.task_repository.replace(staged, expected_updated_at=task.updated_at)

    asyncio.run(stage())
    return task_id


def test_pr_created_follow_up_reopens_and_reanalyzes() -> None:
    app = _app()
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer local-token"}
        task_id = _create_and_publish(app, client, headers)

        response = client.post(
            f"/api/tasks/{task_id}/input",
            headers=headers,
            json={
                "answer": "Manual testing failed on the empty-token path.",
                "follow_up": "fix_request",
            },
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["status"] == "READY"
        assert payload["human_guidance"].startswith("Follow-up (fix requested):")

        reopen = payload["transition_history"][-3]
        assert reopen["from_status"] == "PR_CREATED"
        assert reopen["to_status"] == "ANALYZING"
        assert "published pull request" in reopen["reason"]

        events = {
            event["event_type"]: event
            for event in client.get(f"/api/tasks/{task_id}/history", headers=headers).json()
        }
        assert events["human.input_received"]["metadata"]["follow_up"] == "fix_request"


def test_approved_follow_up_reopens_and_reanalyzes() -> None:
    app = _app()
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer local-token"}
        task_id = _create_and_publish(app, client, headers, status=TaskStatus.APPROVED)

        response = client.post(
            f"/api/tasks/{task_id}/input",
            headers=headers,
            json={
                "answer": "The generated code misses the empty-token path.",
                "follow_up": "fix_request",
            },
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["status"] == "READY"
        assert payload["human_guidance"].startswith("Follow-up (fix requested):")

        reopen = payload["transition_history"][-3]
        assert reopen["from_status"] == "APPROVED"
        assert reopen["to_status"] == "ANALYZING"
        assert "approved task" in reopen["reason"]

        to_ready = client.post(
            f"/api/tasks/{task_id}/transitions",
            headers=headers,
            json={"to_status": "READY", "reason": "Reopen for more work."},
        )
        assert to_ready.status_code == 409


def test_public_transitions_cannot_reopen_a_published_task() -> None:
    app = _app()
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer local-token"}
        task_id = _create_and_publish(app, client, headers)

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


def test_merge_webhook_completes_task_when_follow_up_records_share_the_pr() -> None:
    """Follow-up executions reuse the PR branch, so several records can point at
    the same pull request. The merge webhook must still complete the task using
    the newest record instead of bailing out on an ambiguous match."""

    async def scenario() -> None:
        settings = _settings()
        tasks = MemoryTaskRepository()
        records = create_memory_workflow_repositories()
        now = datetime.now(timezone.utc)
        task = Task(
            id="task-demo",
            title="Fix parser edge case",
            description="Handle an empty token sequence.",
            repository_key="sample",
            task_type=TaskType.BUGFIX,
            status=TaskStatus.PR_CREATED,
            created_at=now,
            updated_at=now,
            created_by="operator-1",
        )
        await tasks.create(task)
        base = dict(
            task_id=task.id,
            repository_key="sample",
            base_branch="main",
            branch_name=BRANCH,
            status=PullRequestStatus.CREATED,
            pr_number=7,
            pr_url=PR_URL,
        )
        old_record = PullRequestRecord(
            id="exec-old",
            execution_id="exec-old",
            patch_fingerprint="a" * 64,
            commit_sha="1" * 40,
            created_at=now,
            updated_at=now,
            **base,
        )
        new_record = PullRequestRecord(
            id="exec-new",
            execution_id="exec-new",
            patch_fingerprint="b" * 64,
            commit_sha="2" * 40,
            created_at=now,
            updated_at=now + timedelta(minutes=1),
            **base,
        )
        await records.pull_requests.create(old_record)
        await records.pull_requests.create(new_record)

        service = GitHubPRService(settings, tasks, records)
        await service.handle_pull_request_webhook(
            {
                "action": "closed",
                "pull_request": {
                    "merged": True,
                    "number": 7,
                    "head": {"ref": BRANCH},
                    "merge_commit_sha": "f" * 40,
                    "merged_at": (now + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
                },
                "repository": {"full_name": "example-org/sample"},
            }
        )

        completed = await tasks.get(task.id)
        assert completed is not None
        assert completed.status == TaskStatus.COMPLETED
        newest = await records.pull_requests.get("exec-new")
        oldest = await records.pull_requests.get("exec-old")
        assert newest is not None and newest.status == PullRequestStatus.MERGED
        assert oldest is not None and oldest.status == PullRequestStatus.CREATED

    asyncio.run(scenario())


def test_lease_push_args_uses_remote_commit_as_explicit_lease() -> None:
    sha = "1" * 40
    listing = f"{sha}\trefs/heads/{BRANCH}\n"
    assert _lease_push_args(BRANCH, listing) == [
        "push",
        f"--force-with-lease=refs/heads/{BRANCH}:{sha}",
        "origin",
        f"HEAD:refs/heads/{BRANCH}",
    ]


def test_lease_push_args_pushes_plainly_when_branch_is_absent() -> None:
    other = f"{'b' * 40}\trefs/heads/some-other-branch\n"
    assert _lease_push_args(BRANCH, "") == [
        "push",
        "origin",
        f"HEAD:refs/heads/{BRANCH}",
    ]
    assert _lease_push_args(BRANCH, other) == [
        "push",
        "origin",
        f"HEAD:refs/heads/{BRANCH}",
    ]


def test_lease_push_args_rejects_malformed_remote_commit_ids() -> None:
    listing = f"not-a-sha\trefs/heads/{BRANCH}\n"
    try:
        _lease_push_args(BRANCH, listing)
    except CodingWorkspaceError:
        pass
    else:
        raise AssertionError("expected CodingWorkspaceError for a malformed commit id")


COMMIT_SHA = "2" * 40
MATCHING_PR = {"number": 7, "state": "open", "head": {"sha": COMMIT_SHA}, "html_url": PR_URL}
STALE_PR = {"number": 7, "state": "open", "head": {"sha": "1" * 40}, "html_url": PR_URL}


class _StubPRClient:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.get_calls = 0

    async def get_pull_request(self, _target, _number: int) -> dict:
        self.get_calls += 1
        return self._responses.pop(0)


def _service() -> GitHubPRService:
    return GitHubPRService(
        _settings(), MemoryTaskRepository(), create_memory_workflow_repositories()
    )


def _record() -> PullRequestRecord:
    now = datetime.now(timezone.utc)
    return PullRequestRecord(
        id="exec-new",
        execution_id="exec-new",
        patch_fingerprint="b" * 64,
        task_id="task-demo",
        repository_key="sample",
        base_branch="main",
        branch_name=BRANCH,
        status=PullRequestStatus.PUSHED,
        commit_sha=COMMIT_SHA,
        created_at=now,
        updated_at=now,
    )


def test_confirm_pushed_pull_request_accepts_a_matching_pull_request() -> None:
    client = _StubPRClient([])
    result = asyncio.run(
        _service()._confirm_pushed_pull_request(
            client,
            RepositoryTarget(owner="example-org", repo="sample", base_branch="main"),
            MATCHING_PR,
            _record(),
            delay=0.0,
        )
    )
    assert result["number"] == 7
    assert client.get_calls == 0


def test_confirm_pushed_pull_request_retries_until_github_serves_the_pushed_commit() -> None:
    """GitHub's PR listing can briefly serve the pre-force-push head commit."""
    client = _StubPRClient([STALE_PR, MATCHING_PR])
    result = asyncio.run(
        _service()._confirm_pushed_pull_request(
            client,
            RepositoryTarget(owner="example-org", repo="sample", base_branch="main"),
            STALE_PR,
            _record(),
            attempts=4,
            delay=0.0,
        )
    )
    assert result["head"]["sha"] == COMMIT_SHA
    assert client.get_calls == 2


def test_confirm_pushed_pull_request_raises_when_the_head_never_matches() -> None:
    client = _StubPRClient([STALE_PR, STALE_PR, STALE_PR])
    try:
        asyncio.run(
            _service()._confirm_pushed_pull_request(
                client,
                RepositoryTarget(owner="example-org", repo="sample", base_branch="main"),
                STALE_PR,
                _record(),
                attempts=3,
                delay=0.0,
            )
        )
    except GitHubOperationError as exc:
        assert "did not match the pushed commit" in str(exc)
    else:
        raise AssertionError("expected GitHubOperationError when the head never matches")
    assert client.get_calls == 2


class _FakeResponse:
    def __init__(self, payload) -> None:
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttp:
    def __init__(self, payload) -> None:
        self._payload = payload

    async def get(self, _url, params=None):
        return _FakeResponse(self._payload)


def test_find_pull_request_prefers_the_open_pull_request_for_a_reused_branch() -> None:
    """An older, closed PR for the same branch must not win over the open one."""
    closed = {"number": 4, "state": "closed"}
    open_pr = {"number": 7, "state": "open"}
    integration = object.__new__(GitHubClient)
    integration.client = _FakeHttp([closed, open_pr])
    target = RepositoryTarget(owner="example-org", repo="sample", base_branch="main")
    found = asyncio.run(integration.find_pull_request(target, BRANCH))
    assert found is not None and found["number"] == 7
