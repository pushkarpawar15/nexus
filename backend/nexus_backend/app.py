from __future__ import annotations

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from .config import Settings, validate_settings
from .models import (
    Task,
    TaskCreate,
    TaskPage,
    TaskPatch,
    TransitionRequest,
    WorkflowEvent,
    WorkflowStatus,
)
from .repositories import (
    ConcurrentTransitionError,
    DuplicateTaskError,
    FirestoreTaskRepository,
    InMemoryTaskRepository,
    TaskNotFoundError,
    TaskRepository,
)
from .services import (
    InvalidTransitionError,
    RepositoryNotApprovedError,
    TaskService,
    WorkflowInputError,
    WorkflowService,
)


def create_repository(settings: Settings) -> TaskRepository:
    validate_settings(settings)
    if settings.task_repository == "memory":
        return InMemoryTaskRepository()
    assert settings.task_repository == "firestore"
    return FirestoreTaskRepository(
        project=settings.google_cloud_project or "",
        database=settings.firestore_database,
        tasks_collection=settings.firestore_tasks_collection,
        events_collection=settings.firestore_events_collection,
    )


def create_app(
    *,
    settings: Settings | None = None,
    repository: TaskRepository | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if repository is None:
        repository = create_repository(settings)
    task_service = TaskService(repository, settings.approved_repository_keys)
    workflow_service = WorkflowService(repository, task_service)

    app = FastAPI(title="NEXUS Task API", version="0.1.0")
    app.state.task_repository = repository
    app.state.task_service = task_service
    app.state.workflow_service = workflow_service

    @app.exception_handler(TaskNotFoundError)
    async def task_not_found_handler(_, exc: TaskNotFoundError):
        return JSONResponse(
            status_code=404,
            content={"detail": f"task '{exc.args[0]}' not found"},
        )

    @app.exception_handler(RepositoryNotApprovedError)
    async def repository_not_approved_handler(_, exc: RepositoryNotApprovedError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(InvalidTransitionError)
    async def invalid_transition_handler(_, exc: InvalidTransitionError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(WorkflowInputError)
    async def workflow_input_handler(_, exc: WorkflowInputError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(ConcurrentTransitionError)
    async def concurrent_transition_handler(_, exc: ConcurrentTransitionError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(DuplicateTaskError)
    async def duplicate_task_handler(_, exc: DuplicateTaskError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.post("/api/tasks", response_model=Task, status_code=201)
    def create_task(request: TaskCreate) -> Task:
        return task_service.create(request)

    @app.get("/api/tasks", response_model=TaskPage)
    def list_tasks(
        status: WorkflowStatus | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> TaskPage:
        return task_service.list(status=status, offset=offset, limit=limit)

    @app.get("/api/tasks/{task_id}", response_model=Task)
    def get_task(task_id: str) -> Task:
        return task_service.get(task_id)

    @app.patch("/api/tasks/{task_id}", response_model=Task)
    def patch_task(task_id: str, request: TaskPatch) -> Task:
        return task_service.patch(task_id, request)

    @app.post("/api/tasks/{task_id}/transitions", response_model=Task)
    def transition_task(task_id: str, request: TransitionRequest) -> Task:
        return workflow_service.transition(task_id, request)

    @app.get("/api/tasks/{task_id}/events", response_model=list[WorkflowEvent])
    def get_task_events(task_id: str) -> list[WorkflowEvent]:
        task_service.get(task_id)
        return repository.list_events(task_id)

    return app
