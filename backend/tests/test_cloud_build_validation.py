import base64
import io
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from app.core.config import Settings
from app.integrations.cloud_build_validation import (
    CloudBuildValidationRunner,
    _build_needs_diagnostics,
    _validation_source_archive,
)
from app.integrations.validation_runner import compute_patch_fingerprint, payflow_commands
from app.models.task import Task, TaskStatus
from app.schemas.execution import ExecutionRecord, ExecutionStatus
from app.schemas.validation import ValidationCommandStatus, ValidationStatus


def test_cloud_validation_skips_all_commands_without_a_profile(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    task = Task(
        id="task-1",
        title="A validation task",
        description="Run configured checks",
        repository_key="github-123",
        status=TaskStatus.IN_PROGRESS,
        created_at=now,
        updated_at=now,
        created_by="operator-1",
    )
    execution = ExecutionRecord(
        id="execution-1",
        task_id=task.id,
        assigned_worker="coding-agent",
        status=ExecutionStatus.QUEUED,
        created_at=now,
        updated_at=now,
        repository_key=task.repository_key,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("source\n", encoding="utf-8")
    settings = Settings(_env_file=None, environment="testing", storage="memory")

    report = CloudBuildValidationRunner(settings).run(
        task,
        execution,
        workspace,
        expected_fingerprint=compute_patch_fingerprint(workspace),
    )

    reason = "No validation profile is configured for this repository."
    assert report.overall_status == ValidationStatus.SKIPPED
    assert report.summary == f"Validation skipped: {reason}"
    assert all(command.status == ValidationCommandStatus.SKIPPED for command in report.commands)
    assert {command.error for command in report.commands} == {reason}


def test_cloud_build_rejection_includes_sanitized_provider_reason() -> None:
    class Response:
        status_code = 400

        @staticmethod
        def json():
            return {
                "error": {
                    "message": "Invalid worker pool; github_token=super-secret",
                }
            }

    class Session:
        @staticmethod
        def post(*_args, **_kwargs):
            return Response()

    settings = Settings(
        _env_file=None,
        environment="testing",
        storage="memory",
        google_cloud_project="demo-project",
        cloud_build_service_account="nexus-build@demo-project.iam.gserviceaccount.com",
        cloud_build_worker_pool=(
            "projects/demo-project/locations/asia-south1/workerPools/nexus-validation"
        ),
    )
    runner = CloudBuildValidationRunner(settings)

    try:
        runner._create_build(
            Session(),
            "demo-project",
            "execution-1",
            "source-bucket",
            payflow_commands(settings),
        )
    except RuntimeError as exc:
        assert str(exc) == (
            "Cloud Build rejected validation (HTTP 400): Invalid worker pool; "
            "github_token=[REDACTED]."
        )
    else:
        raise AssertionError("Cloud Build rejection should include the provider reason")


def test_cloud_build_uses_the_private_pools_regional_endpoint() -> None:
    build_id = UUID("596e40d5-98d8-4c2b-8327-1326cc598993")
    encoded_build_id = base64.urlsafe_b64encode(str(build_id).encode()).decode().rstrip("=")

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"name": f"operations/build/demo-project/{encoded_build_id}"}

    class Session:
        url = None

        def post(self, url, **_kwargs):
            self.url = url
            return Response()

    settings = Settings(
        _env_file=None,
        environment="testing",
        storage="memory",
        google_cloud_project="demo-project",
        cloud_build_validation_location="asia-south1",
        cloud_build_service_account="nexus-build@demo-project.iam.gserviceaccount.com",
        cloud_build_worker_pool=(
            "projects/demo-project/locations/asia-south1/workerPools/nexus-validation"
        ),
    )
    session = Session()

    result = CloudBuildValidationRunner(settings)._create_build(
        session,
        "demo-project",
        "execution-1",
        "source-bucket",
        payflow_commands(settings),
    )

    assert session.url == (
        "https://cloudbuild.googleapis.com/v1/projects/demo-project/locations/asia-south1/builds"
    )
    assert result["name"] == (
        "projects/demo-project/locations/asia-south1/builds/596e40d5-98d8-4c2b-8327-1326cc598993"
    )


def test_temporary_source_bucket_is_readable_by_build_staging_and_step_identities(
    monkeypatch,
) -> None:
    class Bucket:
        def __init__(self):
            self.iam_configuration = type("IamConfiguration", (), {})()
            self.policy = SimpleNamespace(bindings=[])

        def patch(self):
            pass

        def blob(self, _name):
            return type(
                "Blob",
                (),
                {"upload_from_string": lambda *_args, **_kwargs: None},
            )()

        def get_iam_policy(self, requested_policy_version):
            assert requested_policy_version == 3
            return self.policy

        def set_iam_policy(self, policy):
            self.policy = policy

    bucket = Bucket()

    class StorageClient:
        def __init__(self, project):
            assert project == "demo-project"

        def create_bucket(self, _name, location):
            assert location == "asia-south1"
            return bucket

    monkeypatch.setattr("google.cloud.storage.Client", StorageClient)
    settings = Settings(
        _env_file=None,
        environment="testing",
        storage="memory",
        google_cloud_project="demo-project",
        cloud_build_service_agent="service-123@gcp-sa-cloudbuild.iam.gserviceaccount.com",
        cloud_build_service_account="nexus-build@demo-project.iam.gserviceaccount.com",
    )

    CloudBuildValidationRunner(settings)._upload_source("demo-project", "execution-1", b"source")

    assert bucket.policy.bindings == [
        {
            "role": "roles/storage.objectViewer",
            "members": [
                "serviceAccount:service-123@gcp-sa-cloudbuild.iam.gserviceaccount.com",
                "serviceAccount:nexus-build@demo-project.iam.gserviceaccount.com",
            ],
        }
    ]


def test_validation_archive_makes_owner_only_source_files_readable(
    tmp_path,
) -> None:
    source_file = tmp_path / "App.tsx"
    source_file.write_text("export default function App() {}\n", encoding="utf-8")
    source_file.chmod(0o600)

    archive_data = _validation_source_archive([(source_file.relative_to(tmp_path), source_file)])

    with tarfile.open(fileobj=io.BytesIO(archive_data), mode="r:gz") as archive:
        member = archive.getmember("App.tsx")
        assert member.mode & 0o444 == 0o444
        assert member.mode & 0o111 == 0


def test_failed_build_logs_are_attached_to_matching_validation_step() -> None:
    settings = Settings(
        _env_file=None,
        environment="testing",
        storage="memory",
        google_cloud_project="demo-project",
    )
    commands = payflow_commands(settings)
    failed_command = next(
        command for command in commands if command.command_id == "frontend_typecheck"
    )

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "entries": [
                    {
                        "labels": {"build_step": 'Step #5 - "frontend_typecheck"'},
                        "textPayload": "App.tsx(325,35): error TS1127: Invalid character.",
                    },
                    {
                        "labels": {"build_step": 'Step #4 - "frontend_lint"'},
                        "textPayload": "Parsing error: Invalid character",
                    },
                    {
                        "labels": {"build_step": "MAIN"},
                        "textPayload": "unrelated build summary",
                    },
                ]
            }

    class Session:
        request = None

        def post(self, url, **kwargs):
            self.request = (url, kwargs)
            return Response()

    session = Session()
    excerpts = CloudBuildValidationRunner._build_log_excerpts(
        session,
        "demo-project",
        {"id": "build-123"},
        commands,
    )

    assert excerpts == {
        "frontend_lint": "Parsing error: Invalid character",
        "frontend_typecheck": "App.tsx(325,35): error TS1127: Invalid character.",
    }
    url, request = session.request
    assert url == "https://logging.googleapis.com/v2/entries:list"
    assert 'resource.labels.build_id="build-123"' in request["json"]["filter"]

    results = CloudBuildValidationRunner._results(
        commands,
        {
            "steps": [
                {"id": "frontend_typecheck", "status": "FAILURE", "exitCode": 2},
            ]
        },
        excerpts,
    )
    typecheck_result = next(
        result for result in results if result.command_id == failed_command.command_id
    )
    assert typecheck_result.stderr_excerpt == ("App.tsx(325,35): error TS1127: Invalid character.")


def test_cloud_build_success_still_fetches_logs_for_failed_allowed_steps() -> None:
    assert _build_needs_diagnostics(
        {
            "status": "SUCCESS",
            "steps": [
                {"id": "frontend_test", "status": "SUCCESS"},
                {"id": "frontend_typecheck", "status": "FAILURE"},
            ],
        }
    )
    assert _build_needs_diagnostics(
        {
            "status": "SUCCESS",
            "steps": [{"id": "frontend_typecheck", "status": "TIMEOUT"}],
        }
    )
    assert not _build_needs_diagnostics(
        {
            "status": "SUCCESS",
            "steps": [
                {"id": "frontend_test", "status": "SUCCESS"},
                {"id": "frontend_lint", "status": "SUCCESS"},
            ],
        }
    )
