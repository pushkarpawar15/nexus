from __future__ import annotations

import io
import logging
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.core.config import Settings
from app.integrations.validation_runner import (
    MAX_TOTAL_SOURCE_BYTES,
    _source_files,
    compute_patch_fingerprint,
    payflow_commands,
)
from app.models.task import Task
from app.schemas.execution import ExecutionRecord
from app.schemas.validation import (
    ValidationCommandResult,
    ValidationCommandStatus,
    ValidationReport,
    ValidationStatus,
)

logger = logging.getLogger(__name__)


class CloudBuildValidationRunner:
    """Run the fixed PayFlow validation profile in an isolated Cloud Build worker pool."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._active: dict[str, tuple[object, str]] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    def begin_execution(self, execution_id: str) -> None:
        with self._lock:
            self._cancelled.discard(execution_id)

    def cancel_execution(self, execution_id: str) -> None:
        with self._lock:
            self._cancelled.add(execution_id)
            active = self._active.get(execution_id)
        if active:
            session, build_name = active
            try:
                session.post(
                    f"https://cloudbuild.googleapis.com/v1/{build_name}:cancel",
                    json={},
                    timeout=20,
                )
            except Exception:
                logger.exception(
                    "Could not cancel Cloud Build validation (execution_id=%s)", execution_id
                )

    def run(
        self,
        task: Task,
        execution: ExecutionRecord,
        workspace: Path,
        *,
        expected_fingerprint: str,
    ) -> ValidationReport:
        started = datetime.now(timezone.utc)
        results: list[ValidationCommandResult] = []
        overall = ValidationStatus.BLOCKED
        summary = "Cloud validation could not start."
        bucket_name = None
        try:
            if compute_patch_fingerprint(workspace) != expected_fingerprint:
                raise ValueError("Workspace patch fingerprint changed before cloud validation.")
            commands = payflow_commands(self.settings)
            target = self.settings.approved_repositories.get(task.repository_key)
            is_payflow = bool(
                target
                and target.repo.casefold() == "payflow"
                and (workspace / "backend").is_dir()
                and (workspace / "frontend").is_dir()
            )
            if task.repository_key != self.settings.pay_repository_key and not is_payflow:
                results = [
                    self._result(
                        command.command_id,
                        ValidationCommandStatus.UNEXECUTED,
                        "No validation profile is configured for this repository.",
                    )
                    for command in commands
                ]
                summary = "Validation is not configured for this repository."
                return self._report(
                    task,
                    execution,
                    expected_fingerprint,
                    started,
                    results,
                    ValidationStatus.BLOCKED,
                    summary,
                )

            files = _source_files(workspace)
            if sum(path.stat().st_size for _, path in files) > MAX_TOTAL_SOURCE_BYTES:
                raise ValueError("Validation source exceeds the 30 MiB total limit.")
            source = io.BytesIO()
            with tarfile.open(fileobj=source, mode="w:gz") as archive:
                for relative, path in files:
                    info = archive.gettarinfo(str(path), arcname=relative.as_posix())
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    with path.open("rb") as stream:
                        archive.addfile(info, stream)
            project = self.settings.google_cloud_project
            if (
                not project
                or not self.settings.cloud_build_service_account
                or not self.settings.cloud_build_worker_pool
            ):
                raise RuntimeError(
                    "Cloud Build service account and private worker pool are not configured."
                )

            session = self._session()
            bucket_name = self._upload_source(project, execution.id, source.getvalue())
            build = self._create_build(session, project, execution.id, bucket_name, commands)
            build_name = build["name"]
            with self._lock:
                self._active[execution.id] = (session, build_name)
            completed = self._wait_build(session, build_name, execution.id)
            results = self._results(commands, completed)
            if completed.get("status") == "SUCCESS" and all(
                result.status == ValidationCommandStatus.PASSED for result in results
            ):
                overall = ValidationStatus.PASSED
                summary = "All configured cloud validation commands passed."
            elif completed.get("status") == "CANCELLED":
                overall = ValidationStatus.BLOCKED
                summary = "Cloud validation was stopped by an operator."
            elif any(
                result.status
                in {ValidationCommandStatus.FAILED, ValidationCommandStatus.TIMED_OUT}
                for result in results
            ):
                overall = ValidationStatus.FAILED
                summary = (
                    "One or more cloud validation commands failed. "
                    "See the Cloud Build log for details."
                )
            else:
                overall = ValidationStatus.BLOCKED
                summary = "Cloud Build did not complete all validation commands."
            log_url = completed.get("logUrl")
            if log_url:
                summary = f"{summary} Build log: {log_url}"[:2000]
            if compute_patch_fingerprint(workspace) != expected_fingerprint:
                raise ValueError("Workspace patch fingerprint changed during cloud validation.")
        except Exception as exc:
            logger.exception("Cloud Build validation failed (execution_id=%s)", execution.id)
            commands = payflow_commands(self.settings)
            results = [
                self._result(
                    command.command_id,
                    ValidationCommandStatus.UNAVAILABLE,
                    "Cloud validation could not complete; check the worker and Cloud Build settings.",
                )
                for command in commands
            ]
            overall = ValidationStatus.BLOCKED
            summary = str(exc)[:1500]
        finally:
            with self._lock:
                self._active.pop(execution.id, None)
                self._cancelled.discard(execution.id)
            if bucket_name:
                try:
                    self._delete_source(bucket_name)
                except Exception:
                    logger.exception(
                        "Could not remove temporary Cloud Build source (execution_id=%s)",
                        execution.id,
                    )
        return self._report(
            task, execution, expected_fingerprint, started, results, overall, summary
        )

    def _session(self):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        return AuthorizedSession(credentials)

    def _upload_source(self, project: str, execution_id: str, content: bytes) -> str:
        from google.cloud import storage

        suffix = uuid4().hex[:8]
        bucket_name = f"nexus-val-{project[-10:]}-{execution_id[:12]}-{suffix}"[:63].rstrip("-")
        client = storage.Client(project=project)
        bucket = client.create_bucket(
            bucket_name, location=self.settings.cloud_build_validation_location
        )
        bucket.iam_configuration.uniform_bucket_level_access_enabled = True
        bucket.iam_configuration.public_access_prevention = "enforced"
        bucket.patch()
        bucket.blob("source.tar.gz").upload_from_string(content, content_type="application/gzip")
        policy = bucket.get_iam_policy(requested_policy_version=3)
        policy.bindings.append(
            {
                "role": "roles/storage.objectViewer",
                "members": [f"serviceAccount:{self.settings.cloud_build_service_agent}"],
            }
        )
        bucket.set_iam_policy(policy)
        return bucket_name

    def _create_build(self, session, project: str, execution_id: str, bucket: str, commands):
        steps = []
        for command in commands:
            directory = command.cwd.removeprefix("/workspace/")
            steps.append(
                {
                    "id": command.command_id,
                    "name": command.image,
                    "dir": directory,
                    "args": list(command.argv),
                    "allowFailure": True,
                }
            )
        request = {
            "source": {"storageSource": {"bucket": bucket, "object": "source.tar.gz"}},
            "steps": steps,
            "serviceAccount": (
                f"projects/{project}/serviceAccounts/"
                f"{self.settings.cloud_build_service_account}"
            ),
            "options": {
                "logging": "CLOUD_LOGGING_ONLY",
                "pool": {"name": self.settings.cloud_build_worker_pool},
            },
            "timeout": f"{self.settings.cloud_build_timeout_seconds}s",
            "tags": ["nexus-validation", "execution-" + execution_id[:32]],
        }
        response = session.post(
            f"https://cloudbuild.googleapis.com/v1/projects/{project}/builds",
            json=request,
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Cloud Build rejected validation (HTTP {response.status_code}).")
        data = response.json()
        if not data.get("name"):
            raise RuntimeError("Cloud Build did not return a build name.")
        return data

    def _wait_build(self, session, name: str, execution_id: str):
        deadline = time.monotonic() + self.settings.cloud_build_timeout_seconds + 120
        while time.monotonic() < deadline:
            with self._lock:
                cancelled = execution_id in self._cancelled
            if cancelled:
                self.cancel_execution(execution_id)
            response = session.get(f"https://cloudbuild.googleapis.com/v1/{name}", timeout=20)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Could not read Cloud Build status (HTTP {response.status_code})."
                )
            build = response.json()
            if build.get("status") in {
                "SUCCESS",
                "FAILURE",
                "INTERNAL_ERROR",
                "TIMEOUT",
                "CANCELLED",
                "EXPIRED",
            }:
                return build
            time.sleep(3)
        self.cancel_execution(execution_id)
        raise TimeoutError("Cloud Build validation exceeded its configured timeout.")

    def _delete_source(self, bucket_name: str) -> None:
        from google.cloud import storage

        storage.Client().bucket(bucket_name).delete(force=True)

    @staticmethod
    def _results(commands, build):
        status_map = {
            "SUCCESS": ValidationCommandStatus.PASSED,
            "FAILURE": ValidationCommandStatus.FAILED,
            "TIMEOUT": ValidationCommandStatus.TIMED_OUT,
            "CANCELLED": ValidationCommandStatus.UNEXECUTED,
        }
        steps = {step.get("id"): step for step in build.get("steps", [])}
        results = []
        for command in commands:
            step = steps.get(command.command_id)
            if step is None:
                results.append(
                    CloudBuildValidationRunner._result(
                        command.command_id,
                        ValidationCommandStatus.UNEXECUTED,
                        "Cloud Build did not start this command.",
                    )
                )
                continue
            raw = step.get("status", "UNKNOWN")
            status = status_map.get(raw, ValidationCommandStatus.UNAVAILABLE)
            results.append(
                ValidationCommandResult(
                    command_id=command.command_id,
                    status=status,
                    exit_code=(
                        0 if status == ValidationCommandStatus.PASSED else step.get("exitCode")
                    ),
                    error=(
                        None
                        if status == ValidationCommandStatus.PASSED
                        else f"Cloud Build step status: {raw}."
                    ),
                )
            )
        return results

    @staticmethod
    def _result(command_id, status, error):
        return ValidationCommandResult(command_id=command_id, status=status, error=error)

    @staticmethod
    def _report(task, execution, fingerprint, started, results, overall, summary):
        return ValidationReport(
            id=uuid4().hex,
            execution_id=execution.id,
            task_id=task.id,
            patch_fingerprint=fingerprint,
            overall_status=overall,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            commands=results,
            summary=summary[:2000],
        )


__all__ = ["CloudBuildValidationRunner"]
