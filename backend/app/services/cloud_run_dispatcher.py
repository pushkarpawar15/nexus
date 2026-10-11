from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from app.core.config import Settings
from app.repositories.records import WorkflowRepositories

logger = logging.getLogger(__name__)


class CloudRunJobsDispatcher:
    """Dispatch one durable Cloud Run Job execution for each NEXUS execution record."""

    def __init__(self, settings: Settings, records: WorkflowRepositories) -> None:
        self.settings, self.records = settings, records

    def _session(self):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        return AuthorizedSession(credentials)

    @property
    def _job_name(self) -> str:
        return (
            f"projects/{self.settings.google_cloud_project}/locations/"
            f"{self.settings.cloud_run_region}/jobs/{self.settings.cloud_run_job_name}"
        )

    async def dispatch(self, execution_id: str) -> bool:
        try:
            execution_name = await asyncio.to_thread(self._start_job, execution_id)
            execution = await self.records.executions.get(execution_id)
            if execution is not None:
                await self.records.executions.replace(
                    execution.model_copy(
                        update={
                            "cloud_execution_name": execution_name,
                            "updated_at": datetime.now(timezone.utc),
                        }
                    ),
                    expected_updated_at=execution.updated_at,
                )
            return True
        except Exception:
            logger.exception(
                "Could not dispatch Cloud Run execution (execution_id=%s)", execution_id
            )
            return False

    def _start_job(self, execution_id: str) -> str:
        session = self._session()
        response = session.post(
            f"https://run.googleapis.com/v2/{self._job_name}:run",
            json={
                "overrides": {
                    "containerOverrides": [
                        {
                            "env": [
                                {"name": "NEXUS_EXECUTION_ID", "value": execution_id}
                            ]
                        }
                    ],
                    "taskCount": 1,
                    "timeout": f"{self.settings.cloud_run_execution_timeout_seconds}s",
                }
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Cloud Run rejected job dispatch (HTTP {response.status_code}).")
        operation = response.json()
        operation_name = operation.get("name")
        if not isinstance(operation_name, str):
            raise RuntimeError("Cloud Run did not return an execution operation name.")
        operation_url = "https://run.googleapis.com/v2/" + quote(operation_name, safe="/")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            result = session.get(operation_url, timeout=20)
            if result.status_code >= 400:
                raise RuntimeError(
                    f"Could not read Cloud Run dispatch operation (HTTP {result.status_code})."
                )
            operation = result.json()
            if operation.get("done"):
                if operation.get("error"):
                    # Cloud Run may finish the run operation with an execution
                    # error when the container exits during startup. If it
                    # created the execution, dispatch was accepted; the worker
                    # must report its own failure through the execution record.
                    name = self._find_execution(session, execution_id)
                    if name:
                        return name
                    raise RuntimeError("Cloud Run could not start the execution job.")
                response_data = operation.get("response") or {}
                metadata = operation.get("metadata") or {}
                name = response_data.get("name") or metadata.get("name")
                if isinstance(name, str) and "/executions/" in name:
                    return name
                name = self._find_execution(session, execution_id)
                if name:
                    return name
                raise RuntimeError("Cloud Run started the job but returned no execution name.")
            time.sleep(1)
        raise TimeoutError("Cloud Run did not confirm job dispatch within 90 seconds.")

    def _find_execution(self, session: Any, execution_id: str) -> str | None:
        response = session.get(
            f"https://run.googleapis.com/v2/{self._job_name}/executions",
            params={"pageSize": 20},
            timeout=20,
        )
        if response.status_code >= 400:
            return None
        for item in response.json().get("executions", []):
            job_template = item.get("template") or {}
            task_template = job_template.get("template") or {}
            containers = job_template.get("containers") or task_template.get("containers", [])
            for container in containers:
                env = {
                    entry.get("name"): entry.get("value")
                    for entry in container.get("env", [])
                }
                if env.get("NEXUS_EXECUTION_ID") == execution_id:
                    return item.get("name")
        return None

    async def cancel(self, execution_id: str) -> bool:
        execution = await self.records.executions.get(execution_id)
        if execution is None:
            return False
        name = execution.cloud_execution_name
        if not name:
            try:
                name = await asyncio.to_thread(self._find_execution_by_id, execution_id)
            except Exception:
                logger.exception(
                    "Could not locate Cloud Run execution (execution_id=%s)", execution_id
                )
                return False
        if not name:
            return False
        try:
            await asyncio.to_thread(self._cancel_execution, name)
            return True
        except Exception:
            logger.exception(
                "Could not cancel Cloud Run execution (execution_id=%s)", execution_id
            )
            return False

    def _find_execution_by_id(self, execution_id: str) -> str | None:
        return self._find_execution(self._session(), execution_id)

    def _cancel_execution(self, name: str) -> None:
        session = self._session()
        response = session.post(
            f"https://run.googleapis.com/v2/{quote(name, safe='/')}:cancel",
            json={},
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Cloud Run rejected cancellation (HTTP {response.status_code}).")

    async def close(self) -> None:
        return None


__all__ = ["CloudRunJobsDispatcher"]
