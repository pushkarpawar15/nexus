from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.auth import UserIdentity, require_admin, require_operator, require_viewer
from app.core.config import (
    ConfigurationError,
    RepositoryTarget,
    Settings,
    get_settings,
    validate_settings,
)
from app.core.errors import ErrorCode, NexusError
from app.core.logging import CorrelationIdMiddleware, configure_logging, redact_credentials
from app.integrations.attachments import AttachmentStorage
from app.integrations.github_app import GitHubAppError, GitHubAppIntegration
from app.integrations.github_pat import GitHubPATError, GitHubPATIntegration
from app.integrations.github_secrets import (
    GoogleSecretManagerGitHubSecretStore,
    MemoryGitHubSecretStore,
)
from app.models.task import Task, TaskAttachment, TaskPage, TaskStatus
from app.repositories.github_connections import (
    FirestoreGitHubConnectionRepository,
    GitHubRepositoryConnection,
    MemoryGitHubConnectionRepository,
)
from app.repositories.records import (
    create_firestore_workflow_repositories,
    create_memory_workflow_repositories,
)
from app.repositories.skills import FirestoreSkillRepository, MemorySkillRepository
from app.repositories.tasks import (
    FirestoreTaskRepository,
    MemoryTaskRepository,
    TaskRepository,
)
from app.schemas.approval import ApprovalRequest
from app.schemas.execution import ActivityEvent, ActivitySource, ExecutionModelRequest
from app.schemas.github import GitHubPATConnectRequest, GitHubRepositoryRemovalRequest
from app.schemas.skills import SkillRecord, SkillWriteRequest
from app.schemas.tasks import (
    AnalyzeTaskRequest,
    CreateTaskRequest,
    HumanInputRequest,
    SetTaskModeRequest,
    TaskTransitionRequest,
    UpdateExecutionPlanRequest,
    UpdateTaskRequest,
)
from app.services.analysis_service import AnalysisService
from app.services.approval_service import ApprovalService
from app.services.cloud_run_dispatcher import CloudRunJobsDispatcher
from app.services.execution_pipeline import ExecutionPipeline, LocalExecutionDispatcher
from app.services.execution_service import ExecutionService
from app.services.github_pr_service import GitHubPRService
from app.services.langgraph_workflow import AsyncFirestoreCheckpointer
from app.services.model_catalog import model_choices
from app.services.skills import SkillService
from app.services.task_mode_service import TaskModeService
from app.services.task_plan_service import TaskPlanService
from app.services.workflow_service import WorkflowService


def _create_firestore_client(settings: Settings) -> Any:
    try:
        from google.cloud.firestore_v1.async_client import AsyncClient
    except ImportError as exc:  # pragma: no cover - depends on optional runtime install
        raise ConfigurationError(
            "google-cloud-firestore is required when NEXUS_STORAGE=firestore."
        ) from exc

    if settings.firestore_emulator_host:
        os.environ["FIRESTORE_EMULATOR_HOST"] = settings.firestore_emulator_host
    return AsyncClient(
        project=settings.google_cloud_project,
        database=settings.firestore_database,
    )


def create_app(
    *,
    settings: Settings | None = None,
    firestore_client: Any | None = None,
    repository: TaskRepository | None = None,
) -> FastAPI:
    runtime_settings = settings or get_settings()
    validate_settings(runtime_settings)
    configure_logging()
    if repository is not None and runtime_settings.environment == "production":
        raise ConfigurationError("A task repository override is forbidden in production.")
    client = firestore_client
    if repository is None and runtime_settings.storage == "firestore" and client is None:
        client = _create_firestore_client(runtime_settings)
    if repository is not None:
        task_repository = repository
        workflow_repositories = create_memory_workflow_repositories()
        skills_repository = MemorySkillRepository()
        github_connections = MemoryGitHubConnectionRepository()
    elif runtime_settings.storage == "firestore":
        task_repository: TaskRepository = FirestoreTaskRepository(
            client, runtime_settings.approved_repository_keys
        )
        workflow_repositories = create_firestore_workflow_repositories(client)
        skills_repository = FirestoreSkillRepository(client)
        github_connections = FirestoreGitHubConnectionRepository(client)
    else:
        task_repository = MemoryTaskRepository()
        workflow_repositories = create_memory_workflow_repositories()
        skills_repository = MemorySkillRepository()
        github_connections = MemoryGitHubConnectionRepository()

    attachment_storage = AttachmentStorage(runtime_settings)
    github_app = GitHubAppIntegration(runtime_settings, client)
    github_pat = GitHubPATIntegration()
    github_secret_store = (
        GoogleSecretManagerGitHubSecretStore(runtime_settings)
        if runtime_settings.storage == "firestore"
        else MemoryGitHubSecretStore()
    )
    github_pr_service = GitHubPRService(
        runtime_settings, task_repository, workflow_repositories, github_app=github_app
    )
    dispatcher = None
    if runtime_settings.coding_enabled and runtime_settings.validation_enabled:
        if runtime_settings.execution_backend == "cloud_run_jobs":
            dispatcher = CloudRunJobsDispatcher(runtime_settings, workflow_repositories)
        else:
            dispatcher = LocalExecutionDispatcher(
                ExecutionPipeline(
                    runtime_settings,
                    task_repository,
                    workflow_repositories,
                    attachment_storage=attachment_storage,
                    github_app=github_app,
                    github_pr_service=github_pr_service,
                    skills_repository=skills_repository,
                    github_connections=github_connections,
                    github_secret_store=github_secret_store,
                    checkpointer=(
                        AsyncFirestoreCheckpointer(client)
                        if client is not None and runtime_settings.storage == "firestore"
                        else None
                    ),
                )
            )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if dispatcher is not None:
            await dispatcher.close()
        if client is not None:
            close = getattr(client, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result

    app = FastAPI(title="NEXUS API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime_settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept", "X-Correlation-ID"],
    )
    app.add_middleware(CorrelationIdMiddleware)
    app.state.settings = runtime_settings
    app.state.firestore_client = client
    app.state.task_repository = task_repository
    app.state.workflow_repositories = workflow_repositories
    app.state.skills_repository = skills_repository
    app.state.skill_service = SkillService(skills_repository)
    app.state.attachment_storage = attachment_storage
    app.state.github_app = github_app
    app.state.github_pat = github_pat
    app.state.github_connections = github_connections
    app.state.github_secret_store = github_secret_store
    app.state.analysis_service = AnalysisService(
        runtime_settings, task_repository, workflow_repositories
    )
    app.state.task_mode_service = TaskModeService(
        runtime_settings, task_repository, workflow_repositories
    )
    app.state.task_plan_service = TaskPlanService(
        runtime_settings, task_repository, workflow_repositories, attachment_storage
    )
    app.state.approval_service = ApprovalService(
        runtime_settings, task_repository, workflow_repositories
    )
    app.state.github_pr_service = github_pr_service
    app.state.execution_dispatcher = dispatcher
    app.state.execution_service = ExecutionService(
        runtime_settings, task_repository, workflow_repositories, dispatcher
    )
    app.dependency_overrides[get_settings] = lambda: runtime_settings

    @app.middleware("http")
    async def refresh_github_repository_allowlist(request: Request, call_next):
        if (
            runtime_settings.github_app_slug
            and request.url.path.startswith("/api/")
        ):
            try:
                connection = await github_app.status()
                if connection["connected"]:
                    targets = await github_app.repositories()
                    runtime_settings.approved_repositories = {
                        f"github-{target.github_repository_id}": target for target in targets
                    }
                    setter = getattr(task_repository, "set_approved_repository_keys", None)
                    if setter:
                        setter(runtime_settings.approved_repository_keys)
            except GitHubAppError:
                pass
        return await call_next(request)

    @app.exception_handler(NexusError)
    async def nexus_error_handler(_, exc: NexusError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_envelope(),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(_, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "location": [str(part) for part in item.get("loc", ())],
                "message": item.get("msg", "Invalid value"),
            }
            for item in exc.errors()
        ]
        error = NexusError(
            ErrorCode.VALIDATION_ERROR,
            "Request validation failed.",
            status_code=422,
            details=details,
        )
        return JSONResponse(status_code=422, content=error.to_envelope())

    @app.exception_handler(HTTPException)
    async def http_error_handler(_, exc: HTTPException) -> JSONResponse:
        error = NexusError(ErrorCode.HTTP_ERROR, str(exc.detail), status_code=exc.status_code)
        return JSONResponse(
            status_code=exc.status_code, content=error.to_envelope(), headers=exc.headers
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_, exc: Exception) -> JSONResponse:
        import logging

        logging.getLogger("nexus.api").exception("Unhandled API error")
        error = NexusError(
            ErrorCode.INTERNAL_ERROR,
            "An unexpected server error occurred.",
            status_code=500,
        )
        return JSONResponse(status_code=500, content=error.to_envelope())

    @app.middleware("http")
    async def api_version_header(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["X-API-Version"] = "1"
        return response

    async def refresh_internal_repository_allowlist() -> frozenset[str]:
        connections = await github_connections.list_all()
        keys = runtime_settings.approved_repository_keys | frozenset(
            connection.repository_key for connection in connections
        )
        setter = getattr(task_repository, "set_approved_repository_keys", None)
        if setter:
            setter(keys)
        return keys

    async def repository_keys_for_user(user: UserIdentity) -> frozenset[str]:
        connections = await github_connections.list_for_user(user.uid)
        await refresh_internal_repository_allowlist()
        return runtime_settings.approved_repository_keys | frozenset(
            connection.repository_key for connection in connections
        )

    async def repository_target_for_user(
        user: UserIdentity, repository_key: str
    ) -> RepositoryTarget | None:
        connection = await github_connections.get(user.uid, repository_key)
        if connection is not None:
            return connection.target
        return runtime_settings.approved_repositories.get(repository_key)

    async def github_pr_service_for_user(user: UserIdentity, task: Task) -> GitHubPRService:
        connection = await github_connections.get(user.uid, task.repository_key)
        if connection is None:
            return app.state.github_pr_service
        token = await github_secret_store.get(connection.secret_name)
        targets = dict(runtime_settings.approved_repositories)
        targets[task.repository_key] = connection.target
        user_settings = runtime_settings.model_copy(
            update={"github_token": token, "approved_repositories": targets}
        )
        return GitHubPRService(
            user_settings, task_repository, workflow_repositories, github_app=github_app
        )

    async def get_task_or_404(task_id: str, user: UserIdentity) -> Task:
        task = await task_repository.get_for_repository_keys(
            task_id, await repository_keys_for_user(user)
        )
        if task is None:
            raise NexusError(ErrorCode.TASK_NOT_FOUND, "Task was not found.", status_code=404)
        return task

    def repository_connection_payload(connection: GitHubRepositoryConnection) -> dict[str, Any]:
        target = connection.target
        return {
            "key": connection.repository_key,
            "label": target.label or f"{target.owner}/{target.repo}",
            "owner": target.owner,
            "repo": target.repo,
            "base_branch": target.base_branch,
            "private": bool(target.github_private),
            "mode": "pat",
            "account_login": connection.account_login,
        }

    async def pending_task_count(repository_key: str) -> int:
        pending = {
            TaskStatus.BACKLOG,
            TaskStatus.ANALYZING,
            TaskStatus.TRIAGED,
            TaskStatus.READY,
            TaskStatus.HUMAN_QUEUE,
            TaskStatus.IN_PROGRESS,
            TaskStatus.VALIDATING,
            TaskStatus.SECURITY_REVIEW,
            TaskStatus.AWAITING_APPROVAL,
            TaskStatus.AWAITING_INPUT,
            TaskStatus.APPROVED,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
        }
        count = 0
        for task_status in pending:
            _, total = await task_repository.list_for_repository_keys(
                repository_keys=frozenset({repository_key}),
                status=task_status,
                limit=1,
                offset=0,
            )
            count += total
        return count

    async def _validate_skill_repository(
        scope: str, repository_key: str | None, user: UserIdentity
    ) -> None:
        if scope == "repository" and (
            repository_key not in await repository_keys_for_user(user)
        ):
            raise NexusError(
                ErrorCode.REPOSITORY_NOT_ALLOWED,
                "The requested repository is not approved.",
                status_code=422,
            )

    def _authorize_skill_scope(scope: str, user: UserIdentity) -> None:
        if scope == "global" and user.role.value != "admin":
            raise NexusError(
                ErrorCode.HTTP_ERROR,
                "Only administrators can manage global skills.",
                status_code=403,
            )

    @app.get("/api/models")
    async def get_model_choices(_: UserIdentity = Depends(require_viewer)):
        return model_choices(runtime_settings)

    @app.get("/api/repositories")
    async def list_repositories(
        user: UserIdentity = Depends(require_viewer),
    ) -> dict[str, list[dict[str, Any]]]:
        if app.state.github_app.configured:
            connection = await app.state.github_app.status()
            if connection["connected"]:
                try:
                    targets = await app.state.github_app.repositories()
                except GitHubAppError as exc:
                    raise HTTPException(status_code=503, detail=str(exc)) from exc
                runtime_settings.approved_repositories = {
                    f"github-{target.github_repository_id}": target for target in targets
                }
                setter = getattr(task_repository, "set_approved_repository_keys", None)
                if setter:
                    setter(runtime_settings.approved_repository_keys)
        items: dict[str, dict[str, Any]] = {
            key: {
                "key": key,
                "label": target.label or f"{target.owner}/{target.repo}",
                "owner": target.owner,
                "repo": target.repo,
                "base_branch": target.base_branch,
                "private": bool(target.github_private),
                "mode": "app",
            }
            for key, target in runtime_settings.approved_repositories.items()
        }
        for connected in await github_connections.list_for_user(user.uid):
            items[connected.repository_key] = repository_connection_payload(connected)
        return {"items": [items[key] for key in sorted(items)]}

    @app.get("/api/repositories/{repository_key}/removal-impact")
    async def repository_removal_impact(
        repository_key: str,
        user: UserIdentity = Depends(require_operator),
    ) -> dict[str, Any]:
        connection = await github_connections.get(user.uid, repository_key)
        if connection is None:
            raise NexusError(
                ErrorCode.REPOSITORY_NOT_ALLOWED, "Repository was not found.", status_code=404
            )
        return {
            "repository_key": repository_key,
            "pending_task_count": await pending_task_count(repository_key),
        }

    @app.delete("/api/repositories/{repository_key}", status_code=204)
    async def remove_repository(
        repository_key: str,
        request: GitHubRepositoryRemovalRequest,
        user: UserIdentity = Depends(require_operator),
    ) -> Response:
        connection = await github_connections.get(user.uid, repository_key)
        if connection is None:
            raise NexusError(
                ErrorCode.REPOSITORY_NOT_ALLOWED, "Repository was not found.", status_code=404
            )
        pending_count = await pending_task_count(repository_key)
        if pending_count and not request.confirm_pending_tasks:
            raise NexusError(
                ErrorCode.TASK_CONFLICT,
                f"{pending_count} pending task(s) will be hidden until this repository is "
                "reconnected.",
                status_code=409,
            )
        await github_secret_store.delete(connection.secret_name)
        await github_connections.delete(user.uid, repository_key)
        await refresh_internal_repository_allowlist()
        return Response(status_code=204)

    @app.get("/api/github/connection")
    async def github_connection(user: UserIdentity = Depends(require_viewer)):
        pat_connections = await github_connections.list_for_user(user.uid)
        if pat_connections:
            repository = repository_connection_payload(pat_connections[0])
            app_connection = await app.state.github_app.status()
            return {
                "configured": bool(app_connection["configured"]),
                "connected": True,
                "account_login": pat_connections[0].account_login,
                "mode": "pat",
                "repository": repository["label"],
                "repository_info": repository,
                "pat_supported": True,
            }
        result = await app.state.github_app.status()
        result["mode"] = "app" if result["connected"] else None
        result["pat_supported"] = True
        return result

    @app.post("/api/github/pat")
    async def connect_github_pat(
        request: GitHubPATConnectRequest,
        user: UserIdentity = Depends(require_operator),
    ):
        try:
            target, login = await app.state.github_pat.connect(
                request.token.get_secret_value(), request.owner, request.repo
            )
        except GitHubPATError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="Enter a valid GitHub owner and repository."
            ) from exc
        repository_key = f"github-{target.github_repository_id}"
        previous = await github_connections.get(user.uid, repository_key)
        now = datetime.now(timezone.utc)
        secret_digest = hashlib.sha256(f"{user.uid}:{repository_key}".encode()).hexdigest()[:32]
        secret_name = f"nexus-github-{secret_digest}"
        connection = GitHubRepositoryConnection(
            user_id=user.uid,
            account_login=login,
            target=target,
            secret_name=secret_name,
            created_at=previous.created_at if previous else now,
            updated_at=now,
        )
        await github_secret_store.put(secret_name, request.token)
        try:
            await github_connections.put(connection)
        except Exception:
            if previous is None:
                await github_secret_store.delete(secret_name)
            raise
        await refresh_internal_repository_allowlist()
        return {
            "configured": True,
            "connected": True,
            "account_login": login,
            "mode": "pat",
            "repository": target.label,
            "repository_info": repository_connection_payload(connection),
            "pat_supported": True,
        }

    @app.delete("/api/github/pat", status_code=204)
    async def disconnect_github_pat(user: UserIdentity = Depends(require_operator)):
        connections = await github_connections.list_for_user(user.uid)
        for connection in connections:
            if await pending_task_count(connection.repository_key):
                raise NexusError(
                    ErrorCode.TASK_CONFLICT,
                    "Remove repositories individually after reviewing their pending tasks.",
                    status_code=409,
                )
        for connection in connections:
            await github_secret_store.delete(connection.secret_name)
            await github_connections.delete(user.uid, connection.repository_key)
        await refresh_internal_repository_allowlist()
        return Response(status_code=204)

    @app.post("/api/github/install-url")
    async def github_install_url(user: UserIdentity = Depends(require_operator)):
        try:
            return {"url": app.state.github_app.install_url(user.uid)}
        except GitHubAppError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/github/setup", include_in_schema=False)
    async def github_setup_callback(
        installation_id: int = Query(..., ge=1), state: str = Query(..., min_length=1)
    ):
        try:
            account = await app.state.github_app.save_installation(installation_id, state)
            redirect_url = (
                f"{runtime_settings.frontend_url.rstrip('/')}/repositories?connected=1&account="
                f"{__import__('urllib.parse', fromlist=['quote']).quote(account)}"
            )
            return RedirectResponse(redirect_url, status_code=303)
        except GitHubAppError:
            return RedirectResponse(
                f"{runtime_settings.frontend_url.rstrip('/')}/repositories?error=github_setup",
                status_code=303,
            )

    @app.delete("/api/github/connection", status_code=204)
    async def disconnect_github(_: UserIdentity = Depends(require_operator)):
        await app.state.github_app.disconnect()
        runtime_settings.approved_repositories = {}
        setter = getattr(task_repository, "set_approved_repository_keys", None)
        if setter:
            setter(frozenset())
        return Response(status_code=204)

    @app.post("/api/github/webhook", include_in_schema=False)
    async def github_webhook(request: Request):
        body = await request.body()
        signature = request.headers.get("x-hub-signature-256")
        if not github_app.verify_webhook(body, signature):
            raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature.")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid webhook payload.") from exc
        await github_app.handle_webhook(request.headers.get("x-github-event", ""), payload)
        if request.headers.get("x-github-event", "") == "pull_request":
            await github_pr_service.handle_pull_request_webhook(payload)
        return {"received": True}

    @app.post("/api/tasks/{task_id}/attachments", status_code=status.HTTP_201_CREATED)
    async def upload_task_attachments(
        task_id: str,
        files: list[UploadFile] = File(...),
        user: UserIdentity = Depends(require_operator),
    ) -> dict[str, list[TaskAttachment]]:
        task = await get_task_or_404(task_id, user)
        if len(task.attachments) + len(files) > 10:
            raise HTTPException(status_code=413, detail="A task can have at most 10 attachments.")
        accepted_types = {
            "application/pdf",
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/gif",
            "text/plain",
            "text/markdown",
            "text/csv",
            "application/json",
        }
        attachments: list[TaskAttachment] = []
        blobs: list[str] = []
        total = sum(item.size_bytes for item in task.attachments)
        try:
            for upload in files:
                filename = Path(upload.filename or "attachment").name[:240]
                content_type = (upload.content_type or "application/octet-stream").split(";")[0]
                if content_type not in accepted_types:
                    raise HTTPException(
                        status_code=415,
                        detail="Attach PDF, image, plain text, Markdown, CSV, or JSON files.",
                    )
                data = await upload.read(10_000_001)
                if not data or len(data) > 10_000_000:
                    raise HTTPException(
                        status_code=413, detail="Each attachment must be 10 MB or less."
                    )
                total += len(data)
                if total > 20_000_000:
                    raise HTTPException(
                        status_code=413,
                        detail="Task attachments are limited to 20 MB total.",
                    )
                item = TaskAttachment(
                    filename=filename,
                    content_type=content_type,
                    size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    storage_key=f"tasks/{task_id}/{uuid4().hex}",
                    created_at=datetime.now(timezone.utc),
                    created_by=user.uid,
                )
                key = await app.state.attachment_storage.put(task_id, item.id, data, content_type)
                blobs.append(key)
                attachments.append(item.model_copy(update={"storage_key": key}))
            updated = task.model_copy(
                update={
                    "attachments": [*task.attachments, *attachments],
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            await task_repository.replace(updated, expected_updated_at=task.updated_at)
            return {"items": attachments}
        except Exception:
            for key in blobs:
                try:
                    await app.state.attachment_storage.delete(key)
                except Exception:
                    pass
            raise

    @app.get("/api/skills")
    async def list_skills(
        repository_key: str | None = Query(default=None, min_length=1, max_length=150),
        user: UserIdentity = Depends(require_viewer),
    ) -> dict[str, list[SkillRecord]]:
        if repository_key is not None and (
            repository_key not in await repository_keys_for_user(user)
        ):
            raise NexusError(
                ErrorCode.REPOSITORY_NOT_ALLOWED,
                "The requested repository is not approved.",
                status_code=422,
            )
        return {
            "items": await app.state.skill_service.list_for_scope(repository_key),
        }

    @app.post("/api/skills", response_model=SkillRecord, status_code=status.HTTP_201_CREATED)
    async def create_skill(
        request: SkillWriteRequest,
        user: UserIdentity = Depends(require_operator),
    ) -> SkillRecord:
        _authorize_skill_scope(request.scope, user)
        await _validate_skill_repository(request.scope, request.repository_key, user)
        now = datetime.now(timezone.utc)
        record = SkillRecord(
            id=uuid4().hex,
            **request.model_dump(),
            version=1,
            created_at=now,
            updated_at=now,
            created_by=user.uid,
            updated_by=user.uid,
        )
        return await skills_repository.create(record)

    @app.patch("/api/skills/{skill_id}", response_model=SkillRecord)
    async def update_skill(
        skill_id: str,
        request: SkillWriteRequest,
        user: UserIdentity = Depends(require_operator),
    ) -> SkillRecord:
        current = await skills_repository.get(skill_id)
        if current is None:
            raise NexusError(ErrorCode.HTTP_ERROR, "Skill was not found.", status_code=404)
        _authorize_skill_scope(current.scope, user)
        _authorize_skill_scope(request.scope, user)
        await _validate_skill_repository(request.scope, request.repository_key, user)
        now = datetime.now(timezone.utc)
        if now <= current.updated_at:
            from datetime import timedelta

            now = current.updated_at + timedelta(microseconds=1)
        updated = SkillRecord(
            id=current.id,
            **request.model_dump(),
            version=current.version + 1,
            created_at=current.created_at,
            updated_at=now,
            created_by=current.created_by,
            updated_by=user.uid,
        )
        try:
            return await skills_repository.replace(updated, expected_updated_at=current.updated_at)
        except RuntimeError as exc:
            raise NexusError(
                ErrorCode.TASK_CONFLICT, "Skill changed during this request.", status_code=409
            ) from exc

    @app.delete("/api/skills/{skill_id}", status_code=204)
    async def delete_skill(
        skill_id: str,
        user: UserIdentity = Depends(require_operator),
    ) -> None:
        current = await skills_repository.get(skill_id)
        if current is None:
            raise NexusError(ErrorCode.HTTP_ERROR, "Skill was not found.", status_code=404)
        _authorize_skill_scope(current.scope, user)
        try:
            deleted = await skills_repository.delete(
                skill_id, expected_updated_at=current.updated_at
            )
        except RuntimeError as exc:
            raise NexusError(
                ErrorCode.TASK_CONFLICT, "Skill changed during this request.", status_code=409
            ) from exc
        if not deleted:
            raise NexusError(ErrorCode.HTTP_ERROR, "Skill was not found.", status_code=404)

    @app.get("/api/tasks/{task_id}/attachments/{attachment_id}")
    async def download_task_attachment(
        task_id: str, attachment_id: str, user: UserIdentity = Depends(require_viewer)
    ) -> Response:
        task = await get_task_or_404(task_id, user)
        attachment = next((item for item in task.attachments if item.id == attachment_id), None)
        if attachment is None:
            raise HTTPException(status_code=404, detail="Attachment was not found.")
        content = await app.state.attachment_storage.get(attachment.storage_key)
        from urllib.parse import quote

        return Response(
            content=content,
            media_type=attachment.content_type,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(attachment.filename)}",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            },
        )

    @app.delete("/api/tasks/{task_id}/attachments/{attachment_id}", status_code=204)
    async def delete_task_attachment(
        task_id: str, attachment_id: str, user: UserIdentity = Depends(require_operator)
    ) -> Response:
        task = await get_task_or_404(task_id, user)
        attachment = next((item for item in task.attachments if item.id == attachment_id), None)
        if attachment is None:
            raise HTTPException(status_code=404, detail="Attachment was not found.")
        updated = task.model_copy(
            update={
                "attachments": [item for item in task.attachments if item.id != attachment_id],
                "updated_at": datetime.now(timezone.utc),
            }
        )
        await task_repository.replace(updated, expected_updated_at=task.updated_at)
        await app.state.attachment_storage.delete(attachment.storage_key)
        return Response(status_code=204)

    @app.post("/api/tasks", response_model=Task, status_code=status.HTTP_201_CREATED)
    async def create_task(
        request: CreateTaskRequest,
        user: UserIdentity = Depends(require_operator),
    ) -> Task:
        if request.repository_key not in await repository_keys_for_user(user):
            raise NexusError(
                ErrorCode.REPOSITORY_NOT_ALLOWED,
                "The requested repository is not approved.",
                status_code=422,
            )
        now = datetime.now(timezone.utc)
        target = await repository_target_for_user(user, request.repository_key)
        if target is None:
            raise NexusError(
                ErrorCode.REPOSITORY_NOT_ALLOWED,
                "The requested repository is not connected to your account.",
                status_code=422,
            )
        task = Task(
            id=uuid4().hex,
            title=request.title,
            description=request.description,
            repository_key=request.repository_key,
            repository_label=(
                target.label or f"{target.owner}/{target.repo}"
            ),
            task_type=request.task_type,
            status=TaskStatus.BACKLOG,
            created_at=now,
            updated_at=now,
            created_by=user.uid,
        )
        return await task_repository.create(task)

    @app.get("/api/tasks", response_model=TaskPage)
    async def list_tasks(
        status_filter: TaskStatus | None = Query(default=None, alias="status"),
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        user: UserIdentity = Depends(require_viewer),
    ) -> TaskPage:
        items, total = await task_repository.list_for_repository_keys(
            repository_keys=await repository_keys_for_user(user),
            status=status_filter,
            limit=limit,
            offset=offset,
        )
        return TaskPage(items=items, total=total, limit=limit, offset=offset)

    @app.get("/api/tasks/{task_id}", response_model=Task)
    async def get_task(
        task_id: str,
        user: UserIdentity = Depends(require_viewer),
    ) -> Task:
        return await get_task_or_404(task_id, user)

    @app.patch("/api/tasks/{task_id}", response_model=Task)
    async def update_task(
        task_id: str, request: UpdateTaskRequest, user: UserIdentity = Depends(require_operator)
    ) -> Task:
        task = await get_task_or_404(task_id, user)
        if task.status != TaskStatus.BACKLOG:
            raise NexusError(
                ErrorCode.TASK_CONFLICT,
                "Only untouched BACKLOG tasks can be edited.",
                status_code=409,
            )
        if (
            request.repository_key
            and request.repository_key not in await repository_keys_for_user(user)
        ):
            raise NexusError(
                ErrorCode.REPOSITORY_NOT_ALLOWED, "Repository is not approved.", status_code=422
            )
        now = datetime.now(timezone.utc)
        if now <= task.updated_at:
            from datetime import timedelta

            now = task.updated_at + timedelta(microseconds=1)
        updated = task.model_copy(
            update={**request.model_dump(exclude_unset=True), "updated_at": now}
        )
        try:
            return await task_repository.replace(updated, expected_updated_at=task.updated_at)
        except (RuntimeError, KeyError) as exc:
            raise NexusError(
                ErrorCode.TASK_CONFLICT, "Task changed during this request.", status_code=409
            ) from exc

    @app.delete("/api/tasks/{task_id}", status_code=204)
    async def delete_task(task_id: str, user: UserIdentity = Depends(require_operator)) -> None:
        task = await get_task_or_404(task_id, user)
        if task.status != TaskStatus.BACKLOG:
            raise NexusError(
                ErrorCode.DELETION_NOT_ALLOWED,
                "Only BACKLOG tasks can be deleted.",
                status_code=409,
            )
        try:
            await task_repository.delete(task.id, expected_updated_at=task.updated_at)
            for attachment in task.attachments:
                await app.state.attachment_storage.delete(attachment.storage_key)
        except RuntimeError as exc:
            raise NexusError(
                ErrorCode.TASK_CONFLICT, "Task changed during this request.", status_code=409
            ) from exc

    @app.post("/api/tasks/{task_id}/transitions", response_model=Task)
    async def transition_task(
        task_id: str, request: TaskTransitionRequest, user: UserIdentity = Depends(require_operator)
    ) -> Task:
        task = await get_task_or_404(task_id, user)
        if request.to_status == TaskStatus.IN_PROGRESS and not (
            task.status == TaskStatus.HUMAN_QUEUE and task.mode and task.mode.value == "HUMAN"
        ):
            raise NexusError(
                ErrorCode.INVALID_TRANSITION,
                "Only HUMAN_QUEUE work can be manually started.",
                status_code=409,
            )
        return await WorkflowService(task_repository).transition(
            task, request.to_status, actor=user.uid, reason=request.reason
        )

    @app.patch("/api/tasks/{task_id}/mode", response_model=Task)
    async def set_task_mode(
        task_id: str,
        request: SetTaskModeRequest,
        user: UserIdentity = Depends(require_operator),
    ) -> Task:
        task = await get_task_or_404(task_id, user)
        return await app.state.task_mode_service.set_mode(
            task,
            request.mode,
            user.uid,
            risk=request.risk,
            risk_reason=request.risk_reason,
        )

    @app.patch("/api/tasks/{task_id}/plan", response_model=Task)
    async def update_task_plan(
        task_id: str,
        request: UpdateExecutionPlanRequest,
        user: UserIdentity = Depends(require_operator),
    ) -> Task:
        task = await get_task_or_404(task_id, user)
        return await app.state.task_plan_service.update(task, request, user.uid)

    @app.post("/api/tasks/{task_id}/plan/clone", response_model=Task, status_code=201)
    async def clone_task_plan(
        task_id: str,
        request: UpdateExecutionPlanRequest,
        user: UserIdentity = Depends(require_operator),
    ) -> Task:
        task = await get_task_or_404(task_id, user)
        target = await repository_target_for_user(user, task.repository_key)
        return await app.state.task_plan_service.clone(task, request, user.uid, target)

    @app.post("/api/tasks/{task_id}/analyze", response_model=Task)
    async def analyze_task(
        task_id: str,
        request: AnalyzeTaskRequest = AnalyzeTaskRequest(),
        user: UserIdentity = Depends(require_operator),
    ) -> Task:
        task = await get_task_or_404(task_id, user)
        target = await repository_target_for_user(user, task.repository_key)
        return await app.state.analysis_service.analyze(
            task, user.uid, request.model_id, repository_target=target
        )

    @app.post("/api/tasks/{task_id}/executions", response_model=None, status_code=201)
    async def start_execution(
        task_id: str,
        request: ExecutionModelRequest = ExecutionModelRequest(),
        user: UserIdentity = Depends(require_operator),
    ):
        task = await get_task_or_404(task_id, user)
        return await app.state.execution_service.start(task, user.uid, request.model_id)

    @app.post("/api/tasks/{task_id}/executions/retry", response_model=None, status_code=201)
    async def retry_execution(
        task_id: str,
        request: ExecutionModelRequest = ExecutionModelRequest(),
        user: UserIdentity = Depends(require_operator),
    ):
        task = await get_task_or_404(task_id, user)
        return await app.state.execution_service.retry(task, user.uid, request.model_id)

    @app.post("/api/tasks/{task_id}/input", response_model=None, status_code=201)
    async def answer_workflow_question(
        task_id: str,
        request: HumanInputRequest,
        user: UserIdentity = Depends(require_operator),
    ):
        task = await get_task_or_404(task_id, user)
        if task.status == TaskStatus.AWAITING_INPUT and task.pending_execution_id is None:
            guidance = "\n\n".join(
                value
                for value in (
                    task.human_guidance,
                    redact_credentials(request.answer.strip()),
                )
                if value
            )[-8000:]
            updated = task.model_copy(
                update={"human_guidance": guidance, "updated_at": datetime.now(timezone.utc)}
            )
            try:
                await task_repository.replace(updated, expected_updated_at=task.updated_at)
            except RuntimeError as exc:
                raise NexusError(
                    ErrorCode.TASK_CONFLICT,
                    "Task changed while operator guidance was being submitted.",
                    status_code=409,
                ) from exc
            await workflow_repositories.activity_events.create(
                ActivityEvent(
                    id=uuid4().hex,
                    task_id=task.id,
                    execution_id=None,
                    worker_name=user.uid,
                    event_type="human.input_received",
                    message="Operator clarification was saved and sent to the planner.",
                    occurred_at=datetime.now(timezone.utc),
                    source=ActivitySource.SYSTEM,
                    metadata={"guidance": redact_credentials(request.answer.strip())},
                )
            )
            target = await repository_target_for_user(user, updated.repository_key)
            return await app.state.analysis_service.analyze(
                updated, user.uid, repository_target=target
            )
        return await app.state.execution_service.provide_input(task, request.answer, user.uid)

    @app.post("/api/tasks/{task_id}/executions/{execution_id}/stop")
    async def stop_execution(
        task_id: str,
        execution_id: str,
        user: UserIdentity = Depends(require_operator),
    ):
        task = await get_task_or_404(task_id, user)
        return await app.state.execution_service.stop(task, execution_id, user.uid)

    @app.get("/api/tasks/{task_id}/executions")
    async def list_task_executions(
        task_id: str, user: UserIdentity = Depends(require_viewer)
    ):
        await get_task_or_404(task_id, user)
        records = await workflow_repositories.executions.list_for_task(task_id)
        return sorted(records, key=lambda item: (item.created_at, item.id))

    @app.get("/api/executions/{execution_id}")
    async def get_execution(execution_id: str, user: UserIdentity = Depends(require_viewer)):
        record = await workflow_repositories.executions.get(execution_id)
        if record is None:
            raise NexusError(
                ErrorCode.EXECUTION_NOT_FOUND, "Execution was not found.", status_code=404
            )
        await get_task_or_404(record.task_id, user)
        return record

    @app.get("/api/tasks/{task_id}/history")
    async def task_history(task_id: str, user: UserIdentity = Depends(require_viewer)):
        await get_task_or_404(task_id, user)
        events = await workflow_repositories.activity_events.list_for_task(task_id)
        return sorted(events, key=lambda event: (event.occurred_at, event.id))

    @app.get("/api/executions/{execution_id}/activity")
    async def execution_activity(
        execution_id: str, user: UserIdentity = Depends(require_viewer)
    ):
        record = await workflow_repositories.executions.get(execution_id)
        if record is None:
            raise NexusError(
                ErrorCode.EXECUTION_NOT_FOUND, "Execution was not found.", status_code=404
            )
        await get_task_or_404(record.task_id, user)
        events = await workflow_repositories.activity_events.list_for_task(record.task_id)
        return sorted(
            (event for event in events if event.execution_id == execution_id),
            key=lambda event: (event.occurred_at, event.id),
        )

    @app.get("/api/activity")
    async def recent_activity(
        limit: int = Query(default=50, ge=1, le=200),
        user: UserIdentity = Depends(require_viewer),
    ):
        events = await workflow_repositories.activity_events.list_all(limit=5000)
        visible_tasks, _ = await task_repository.list_for_repository_keys(
            repository_keys=await repository_keys_for_user(user),
            status=None,
            limit=5000,
            offset=0,
        )
        visible_ids = {task.id for task in visible_tasks}
        events = [event for event in events if event.task_id in visible_ids]
        events.sort(key=lambda event: (event.occurred_at, event.id), reverse=True)
        return events[:limit]

    @app.get("/api/executions/{execution_id}/validation")
    async def get_validation(execution_id: str, user: UserIdentity = Depends(require_viewer)):
        record = await workflow_repositories.executions.get(execution_id)
        if record is None:
            raise NexusError(
                ErrorCode.EXECUTION_NOT_FOUND, "Execution was not found.", status_code=404
            )
        await get_task_or_404(record.task_id, user)
        reports = await workflow_repositories.validation_reports.list_for_task(record.task_id)
        matches = [report for report in reports if report.execution_id == execution_id]
        return max(matches, key=lambda item: (item.finished_at, item.id), default=None)

    @app.get("/api/executions/{execution_id}/security")
    async def get_security(execution_id: str, user: UserIdentity = Depends(require_viewer)):
        record = await workflow_repositories.executions.get(execution_id)
        if record is None:
            raise NexusError(
                ErrorCode.EXECUTION_NOT_FOUND, "Execution was not found.", status_code=404
            )
        await get_task_or_404(record.task_id, user)
        reports = await workflow_repositories.security_reviews.list_for_task(record.task_id)
        matches = [report for report in reports if report.execution_id == execution_id]
        return max(matches, key=lambda item: (item.finished_at, item.id), default=None)

    @app.get("/api/executions/{execution_id}/diff")
    async def get_execution_diff(execution_id: str, user: UserIdentity = Depends(require_viewer)):
        execution = await workflow_repositories.executions.get(execution_id)
        if execution is None:
            raise NexusError(
                ErrorCode.EXECUTION_NOT_FOUND, "Execution was not found.", status_code=404
            )
        task = await get_task_or_404(execution.task_id, user)
        if task is None or execution.repository_key != task.repository_key:
            raise NexusError(
                ErrorCode.PUBLICATION_BLOCKED,
                "Execution repository evidence does not match its task.",
                status_code=409,
            )
        pull_request = await workflow_repositories.pull_requests.get(execution_id)
        from app.services.execution_diff import ExecutionDiffError, read_execution_diff

        try:
            target = await repository_target_for_user(user, task.repository_key)
            diff_settings = (
                runtime_settings.model_copy(
                    update={
                        "approved_repositories": {
                            **runtime_settings.approved_repositories,
                            task.repository_key: target,
                        }
                    }
                )
                if target is not None
                else runtime_settings
            )
            return await asyncio.to_thread(
                read_execution_diff, diff_settings, execution, pull_request
            )
        except ExecutionDiffError as exc:
            raise NexusError(ErrorCode.PUBLICATION_BLOCKED, str(exc), status_code=409) from exc

    @app.get("/api/tasks/{task_id}/approvals")
    async def list_task_approvals(task_id: str, user: UserIdentity = Depends(require_viewer)):
        await get_task_or_404(task_id, user)
        records = await workflow_repositories.approvals.list_for_task(task_id)
        return sorted(records, key=lambda item: (item.decided_at, item.id))

    @app.post("/api/tasks/{task_id}/executions/{execution_id}/approval", status_code=201)
    async def decide_approval(
        task_id: str,
        execution_id: str,
        request: ApprovalRequest,
        user: UserIdentity = Depends(require_admin),
    ):
        task = await get_task_or_404(task_id, user)
        decision = await app.state.approval_service.decide(task_id, execution_id, user.uid, request)
        if decision.status.value == "APPROVED":
            try:
                service = await github_pr_service_for_user(user, task)
                await service.create(task_id, execution_id)
            except Exception:
                # The approval is durable; the task detail view keeps a retry action for PRs.
                pass
        return decision

    @app.get("/api/executions/{execution_id}/pull-request")
    async def get_pull_request(execution_id: str, user: UserIdentity = Depends(require_viewer)):
        execution = await workflow_repositories.executions.get(execution_id)
        if execution is None:
            raise NexusError(
                ErrorCode.EXECUTION_NOT_FOUND, "Execution was not found.", status_code=404
            )
        await get_task_or_404(execution.task_id, user)
        return await workflow_repositories.pull_requests.get(execution_id)

    @app.get("/api/executions/{execution_id}/pull-request-eligibility")
    async def pull_request_eligibility(
        execution_id: str, user: UserIdentity = Depends(require_viewer)
    ):
        execution = await workflow_repositories.executions.get(execution_id)
        if execution is None:
            raise NexusError(
                ErrorCode.EXECUTION_NOT_FOUND, "Execution was not found.", status_code=404
            )
        task = await get_task_or_404(execution.task_id, user)
        service = await github_pr_service_for_user(user, task)
        eligible, reason = await service.eligibility(execution.task_id, execution_id)
        return {"eligible": eligible, "reason": reason}

    @app.post("/api/tasks/{task_id}/executions/{execution_id}/pull-request", status_code=201)
    async def create_pull_request(
        task_id: str, execution_id: str, user: UserIdentity = Depends(require_operator)
    ):
        task = await get_task_or_404(task_id, user)
        service = await github_pr_service_for_user(user, task)
        return await service.create(task_id, execution_id)

    @app.post("/api/tasks/{task_id}/executions/{execution_id}/pull-request/sync")
    async def sync_pull_request(
        task_id: str, execution_id: str, user: UserIdentity = Depends(require_operator)
    ):
        task = await get_task_or_404(task_id, user)
        service = await github_pr_service_for_user(user, task)
        return await service.sync_pull_request(task_id, execution_id)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    async def readiness():
        try:
            await asyncio.wait_for(task_repository.ping(), timeout=3)
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", "store": runtime_settings.storage},
            )
        return {"status": "ready"}

    app.add_api_route("/api/ready", readiness, methods=["GET"])
    app.add_api_route("/ready", readiness, methods=["GET"], include_in_schema=False)
    return app
