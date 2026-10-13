from __future__ import annotations

import asyncio
import inspect
import logging
import os
import signal
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.integrations.attachments import AttachmentStorage
from app.integrations.cloud_build_validation import CloudBuildValidationRunner
from app.integrations.github_app import GitHubAppIntegration
from app.integrations.workspace_artifact import upload_workspace
from app.repositories.records import create_firestore_workflow_repositories
from app.repositories.tasks import FirestoreTaskRepository
from app.schemas.execution import ExecutionStatus
from app.schemas.pull_request import PullRequestStatus
from app.services.execution_pipeline import ExecutionPipeline
from app.services.github_pr_service import GitHubPRService
from app.services.langgraph_workflow import AsyncFirestoreCheckpointer
from app.services.validation_service import ValidationService


async def run_worker() -> None:
    execution_id = os.environ.get("NEXUS_EXECUTION_ID", "").strip()
    if not execution_id:
        raise RuntimeError("NEXUS_EXECUTION_ID was not provided by the Cloud Run dispatcher.")

    from google.cloud.firestore_v1.async_client import AsyncClient

    settings = get_settings()
    client = AsyncClient(
        project=settings.google_cloud_project,
        database=settings.firestore_database,
    )
    validation_runner = CloudBuildValidationRunner(settings)
    try:
        records = create_firestore_workflow_repositories(client)
        execution = await records.executions.get(execution_id)
        if execution is None:
            logging.getLogger(__name__).error(
                "Execution record is missing (execution_id=%s)", execution_id
            )
            return
        github_app = GitHubAppIntegration(settings, client)
        if github_app.configured:
            targets = await github_app.repositories()
            allowed_targets = {f"github-{item.github_repository_id}": item for item in targets}
        else:
            allowed_targets = dict(settings.approved_repositories)
        task_repository = FirestoreTaskRepository(client, frozenset(allowed_targets))
        task = await task_repository.get(execution.task_id)
        if task is None:
            raise RuntimeError("Task repository is no longer installed or approved in GitHub.")
        settings.approved_repositories = allowed_targets
        settings.workspace_root = settings.workspace_root or Path("/tmp/nexus-workspaces")
        settings.workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)

        attachment_storage = AttachmentStorage(settings)
        github_pr_service = GitHubPRService(
            settings, task_repository, records, github_app=github_app
        )
        validation_service = ValidationService(
            settings, task_repository, records, runner=validation_runner
        )
        pipeline = ExecutionPipeline(
            settings,
            task_repository,
            records,
            validator=validation_service,
            attachment_storage=attachment_storage,
            github_app=github_app,
            github_pr_service=github_pr_service,
            checkpointer=AsyncFirestoreCheckpointer(client),
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:

                def stop_worker() -> None:
                    validation_runner.cancel_execution(execution_id)
                    asyncio.create_task(pipeline.stop_active_processes(execution_id))

                loop.add_signal_handler(sig, stop_worker)
            except NotImplementedError:
                pass
        await pipeline.run(execution_id)
        finished = await records.executions.get(execution_id)
        pull_request = await records.pull_requests.get(execution_id)
        if (
            finished
            and finished.status == ExecutionStatus.SUCCEEDED
            and not (pull_request and pull_request.status == PullRequestStatus.CREATED)
            and finished.workspace_reference
            and Path(finished.workspace_reference).is_dir()
        ):
            artifact = await asyncio.to_thread(
                upload_workspace, settings, execution_id, Path(finished.workspace_reference)
            )
            await records.executions.replace(
                finished.model_copy(
                    update={
                        "workspace_reference": artifact,
                        "updated_at": datetime.now(timezone.utc),
                    }
                ),
                expected_updated_at=finished.updated_at,
            )
    finally:
        close_result = client.close()
        if inspect.isawaitable(close_result):
            await close_result


def main() -> None:
    configure_logging()
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
