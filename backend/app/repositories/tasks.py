from __future__ import annotations

from asyncio import Lock
from copy import deepcopy
from typing import Any, Protocol

from app.models.task import Task, TaskStatus


class TaskRepository(Protocol):
    async def create(self, task: Task) -> Task: ...

    async def get(self, task_id: str) -> Task | None: ...

    async def list(
        self, *, status: TaskStatus | None, limit: int, offset: int
    ) -> tuple[list[Task], int]: ...

    async def replace(self, task: Task, *, expected_updated_at: Any) -> Task: ...

    async def delete(self, task_id: str, *, expected_updated_at: Any) -> bool: ...

    async def ping(self) -> None: ...


class MemoryTaskRepository:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = Lock()

    async def create(self, task: Task) -> Task:
        async with self._lock:
            if task.id in self._tasks:
                raise ValueError("task already exists")
            self._tasks[task.id] = deepcopy(task)
            return deepcopy(task)

    async def get(self, task_id: str) -> Task | None:
        async with self._lock:
            task = self._tasks.get(task_id)
            return deepcopy(task) if task is not None else None

    async def list(
        self, *, status: TaskStatus | None, limit: int, offset: int
    ) -> tuple[list[Task], int]:
        async with self._lock:
            tasks = list(self._tasks.values())
            if status is not None:
                tasks = [task for task in tasks if task.status == status]
            tasks.sort(key=lambda task: (task.created_at, task.id), reverse=True)
            return deepcopy(tasks[offset : offset + limit]), len(tasks)

    async def replace(self, task: Task, *, expected_updated_at: Any) -> Task:
        async with self._lock:
            current = self._tasks.get(task.id)
            if current is None or current.updated_at != expected_updated_at:
                raise RuntimeError("task changed concurrently")
            self._tasks[task.id] = deepcopy(task)
            return deepcopy(task)

    async def delete(self, task_id: str, *, expected_updated_at: Any) -> bool:
        async with self._lock:
            current = self._tasks.get(task_id)
            if current is None:
                return False
            if current.updated_at != expected_updated_at:
                raise RuntimeError("task changed concurrently")
            del self._tasks[task_id]
            return True

    async def ping(self) -> None:
        return None


class FirestoreTaskRepository:
    def __init__(self, client: Any, approved_repository_keys: frozenset[str]) -> None:
        self._client = client
        self._collection = client.collection("tasks")
        self._approved_repository_keys = approved_repository_keys

    def set_approved_repository_keys(self, keys: frozenset[str]) -> None:
        self._approved_repository_keys = keys

    async def create(self, task: Task) -> Task:
        await self._collection.document(task.id).create(task.model_dump(mode="json"))
        return task

    async def get(self, task_id: str) -> Task | None:
        snapshot = await self._collection.document(task_id).get()
        if not snapshot.exists:
            return None
        task = Task.model_validate(snapshot.to_dict())
        if task.repository_key not in self._approved_repository_keys:
            return None
        return task

    async def list(
        self, *, status: TaskStatus | None, limit: int, offset: int
    ) -> tuple[list[Task], int]:
        if not self._approved_repository_keys:
            return [], 0
        keys = sorted(self._approved_repository_keys)
        snapshots = []
        for start in range(0, len(keys), 30):
            query = self._collection.where("repository_key", "in", keys[start : start + 30])
            if status is not None:
                query = query.where("status", "==", status.value)
            snapshots.extend([snapshot async for snapshot in query.stream()])
        tasks = [Task.model_validate(snapshot.to_dict()) for snapshot in snapshots]
        tasks.sort(key=lambda task: (task.created_at, task.id), reverse=True)
        return tasks[offset : offset + limit], len(tasks)

    async def replace(self, task: Task, *, expected_updated_at: Any) -> Task:
        from google.cloud.firestore_v1.async_transaction import async_transactional

        reference = self._collection.document(task.id)
        transaction = self._client.transaction()

        @async_transactional
        async def replace_in_transaction(transaction):
            snapshot = await reference.get(transaction=transaction)
            if not snapshot.exists:
                raise KeyError(task.id)
            current = Task.model_validate(snapshot.to_dict())
            if current.updated_at != expected_updated_at:
                raise RuntimeError("task changed concurrently")
            transaction.set(reference, task.model_dump(mode="json"))

        await replace_in_transaction(transaction)
        return task

    async def delete(self, task_id: str, *, expected_updated_at: Any) -> bool:
        from google.cloud.firestore_v1.async_transaction import async_transactional

        reference = self._collection.document(task_id)
        transaction = self._client.transaction()
        deleted = False

        @async_transactional
        async def delete_in_transaction(transaction):
            nonlocal deleted
            snapshot = await reference.get(transaction=transaction)
            if not snapshot.exists:
                return
            current = Task.model_validate(snapshot.to_dict())
            if current.updated_at != expected_updated_at:
                raise RuntimeError("task changed concurrently")
            transaction.delete(reference)
            deleted = True

        await delete_in_transaction(transaction)
        return deleted

    async def ping(self) -> None:
        await self._collection.document("nexus-readiness").get()


__all__ = ["FirestoreTaskRepository", "MemoryTaskRepository", "TaskRepository"]
