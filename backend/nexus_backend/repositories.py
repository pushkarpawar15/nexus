from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import Protocol

from .models import Task, WorkflowEvent, WorkflowStatus


class TaskNotFoundError(LookupError):
    pass


class DuplicateTaskError(RuntimeError):
    pass


class ConcurrentTransitionError(RuntimeError):
    pass


class TaskRepository(Protocol):
    """Persistence boundary used by task and workflow services."""

    def create(self, task: Task) -> Task: ...

    def get(self, task_id: str) -> Task | None: ...

    def list(
        self,
        *,
        repository_keys: frozenset[str],
        status: WorkflowStatus | None,
        offset: int,
        limit: int,
    ) -> tuple[list[Task], bool]: ...

    def update(self, task: Task) -> Task: ...

    def transition(
        self,
        *,
        expected_status: WorkflowStatus,
        task: Task,
        event: WorkflowEvent,
    ) -> Task: ...

    def record_event(self, event: WorkflowEvent) -> None: ...

    def list_events(self, task_id: str) -> list[WorkflowEvent]: ...


class InMemoryTaskRepository:
    """Thread-safe deterministic adapter intended for tests and explicit local use."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._events: list[WorkflowEvent] = []
        self._lock = RLock()

    def create(self, task: Task) -> Task:
        with self._lock:
            if task.id in self._tasks:
                raise DuplicateTaskError(f"task '{task.id}' already exists")
            self._tasks[task.id] = deepcopy(task)
            return deepcopy(task)

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return deepcopy(task) if task is not None else None

    def list(
        self,
        *,
        repository_keys: frozenset[str],
        status: WorkflowStatus | None,
        offset: int,
        limit: int,
    ) -> tuple[list[Task], bool]:
        with self._lock:
            tasks = [
                task for task in self._tasks.values() if task.repository_key in repository_keys
            ]
            if status is not None:
                tasks = [task for task in tasks if task.status == status]
            tasks.sort(key=lambda task: (task.created_at, task.id), reverse=True)
            page = tasks[offset : offset + limit + 1]
            has_more = len(page) > limit
            return deepcopy(page[:limit]), has_more

    def update(self, task: Task) -> Task:
        with self._lock:
            if task.id not in self._tasks:
                raise TaskNotFoundError(task.id)
            self._tasks[task.id] = deepcopy(task)
            return deepcopy(task)

    def transition(
        self,
        *,
        expected_status: WorkflowStatus,
        task: Task,
        event: WorkflowEvent,
    ) -> Task:
        with self._lock:
            current = self._tasks.get(task.id)
            if current is None:
                raise TaskNotFoundError(task.id)
            if current.status != expected_status:
                raise ConcurrentTransitionError(
                    f"expected {expected_status}, found {current.status}"
                )
            self._tasks[task.id] = deepcopy(task)
            self._events.append(deepcopy(event))
            return deepcopy(task)

    def record_event(self, event: WorkflowEvent) -> None:
        with self._lock:
            self._events.append(deepcopy(event))

    def list_events(self, task_id: str) -> list[WorkflowEvent]:
        with self._lock:
            events = [event for event in self._events if event.task_id == task_id]
            return deepcopy(events)


class FirestoreTaskRepository:
    """Firestore adapter with atomic task-state and successful-event writes."""

    def __init__(
        self,
        *,
        project: str,
        database: str,
        tasks_collection: str,
        events_collection: str,
        client=None,
    ) -> None:
        try:
            from google.cloud import firestore
        except ImportError as exc:  # pragma: no cover - exercised in deployment setup
            raise RuntimeError(
                "google-cloud-firestore is required for the Firestore adapter"
            ) from exc

        self._firestore = firestore
        self._client = client or firestore.Client(project=project, database=database)
        self._tasks = self._client.collection(tasks_collection)
        self._events = self._client.collection(events_collection)

    def create(self, task: Task) -> Task:
        self._tasks.document(task.id).create(_to_document(task))
        return task

    def get(self, task_id: str) -> Task | None:
        snapshot = self._tasks.document(task_id).get()
        if not snapshot.exists:
            return None
        return Task.model_validate(snapshot.to_dict())

    def list(
        self,
        *,
        repository_keys: frozenset[str],
        status: WorkflowStatus | None,
        offset: int,
        limit: int,
    ) -> tuple[list[Task], bool]:
        if not repository_keys:
            return [], False
        query = self._tasks
        query = query.where("repository_key", "in", sorted(repository_keys))
        if status is not None:
            query = query.where("status", "==", status.value)
        query = query.order_by("created_at", direction=self._firestore.Query.DESCENDING)
        snapshots = list(query.offset(offset).limit(limit + 1).stream())
        has_more = len(snapshots) > limit
        return [Task.model_validate(item.to_dict()) for item in snapshots[:limit]], has_more

    def update(self, task: Task) -> Task:
        reference = self._tasks.document(task.id)
        snapshot = reference.get()
        if not snapshot.exists:
            raise TaskNotFoundError(task.id)
        reference.set(_to_document(task))
        return task

    def transition(
        self,
        *,
        expected_status: WorkflowStatus,
        task: Task,
        event: WorkflowEvent,
    ) -> Task:
        transaction = self._client.transaction()
        task_ref = self._tasks.document(task.id)
        event_ref = self._events.document(event.id)
        firestore = self._firestore

        @firestore.transactional
        def apply(transaction):
            snapshot = task_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise TaskNotFoundError(task.id)
            current = Task.model_validate(snapshot.to_dict())
            if current.status != expected_status:
                raise ConcurrentTransitionError(
                    f"expected {expected_status}, found {current.status}"
                )
            transaction.set(task_ref, _to_document(task))
            transaction.create(event_ref, _to_document(event))

        apply(transaction)
        return task

    def record_event(self, event: WorkflowEvent) -> None:
        self._events.document(event.id).create(_to_document(event))

    def list_events(self, task_id: str) -> list[WorkflowEvent]:
        query = self._events.where("task_id", "==", task_id).order_by("occurred_at")
        return [WorkflowEvent.model_validate(item.to_dict()) for item in query.stream()]


def _to_document(model):
    """Firestore accepts JSON-compatible scalar/list/dict values and timestamps."""
    return model.model_dump(mode="json")
