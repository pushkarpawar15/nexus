from __future__ import annotations

from asyncio import Lock
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel

RecordT = TypeVar("RecordT", bound=BaseModel)


def _record_sort_key(record: BaseModel) -> tuple[int, str, str]:
    timestamp = (
        getattr(record, "created_at", None)
        or getattr(record, "occurred_at", None)
        or getattr(record, "decided_at", None)
    )
    if hasattr(timestamp, "isoformat"):
        timestamp = timestamp.isoformat()
    return (
        1 if timestamp is not None else 0,
        str(timestamp or ""),
        str(getattr(record, "id", "")),
    )


class RecordRepository(Protocol, Generic[RecordT]):
    async def create(self, record: RecordT) -> RecordT: ...

    async def create_or_get(self, record: RecordT) -> RecordT: ...

    async def get(self, record_id: str) -> RecordT | None: ...

    async def list_for_task(self, task_id: str) -> list[RecordT]: ...

    async def replace(self, record: RecordT, *, expected_updated_at: Any = None) -> RecordT: ...

    async def list_all(self, *, limit: int | None = None) -> list[RecordT]: ...


class ImmutableRecordRepository(Protocol, Generic[RecordT]):
    async def create(self, record: RecordT) -> RecordT: ...

    async def create_or_get(self, record: RecordT) -> RecordT: ...

    async def get(self, record_id: str) -> RecordT | None: ...

    async def list_for_task(self, task_id: str) -> list[RecordT]: ...

    async def list_for_task_page(
        self, task_id: str, *, limit: int, before: str | None = None
    ) -> list[RecordT]: ...


class FirestoreRecordRepository(Generic[RecordT]):
    """Typed adapter for one top-level workflow collection."""

    def __init__(self, client: Any, collection_name: str, model_type: type[RecordT]) -> None:
        self._client = client
        self.collection_name = collection_name
        self.model_type = model_type
        self._collection = client.collection(collection_name)

    async def create(self, record: RecordT) -> RecordT:
        await self._collection.document(record.id).create(record.model_dump(mode="json"))
        return record

    async def create_or_get(self, record: RecordT) -> RecordT:
        existing = await self.get(record.id)
        if existing is not None:
            return existing
        try:
            return await self.create(record)
        except Exception:
            existing = await self.get(record.id)
            if existing is None:
                raise
            return existing

    async def get(self, record_id: str) -> RecordT | None:
        snapshot = await self._collection.document(record_id).get()
        return self.model_type.model_validate(snapshot.to_dict()) if snapshot.exists else None

    async def list_for_task(self, task_id: str) -> list[RecordT]:
        query = self._collection.where("task_id", "==", task_id)
        snapshots = [snapshot async for snapshot in query.stream()]
        records = [self.model_type.model_validate(item.to_dict()) for item in snapshots]
        records.sort(key=_record_sort_key, reverse=True)
        return records

    async def list_for_task_page(
        self, task_id: str, *, limit: int, before: str | None = None
    ) -> list[RecordT]:
        from google.cloud.firestore_v1.base_query import BaseQuery

        query = self._collection.where("task_id", "==", task_id)
        if before is not None:
            query = query.where("occurred_at", "<", before)
        query = query.order_by("occurred_at", direction=BaseQuery.DESCENDING).limit(limit)
        snapshots = [snapshot async for snapshot in query.stream()]
        records = [self.model_type.model_validate(item.to_dict()) for item in snapshots]
        records.sort(key=_record_sort_key, reverse=True)
        return records

    async def replace(self, record: RecordT, *, expected_updated_at: Any = None) -> RecordT:
        reference = self._collection.document(record.id)
        if expected_updated_at is None:
            await reference.set(record.model_dump(mode="json"))
            return record
        from google.cloud.firestore_v1.async_transaction import async_transactional

        transaction = self._client.transaction()

        @async_transactional
        async def replace_in_transaction(transaction):
            snapshot = await reference.get(transaction=transaction)
            if not snapshot.exists:
                raise KeyError(record.id)
            current = self.model_type.model_validate(snapshot.to_dict())
            if getattr(current, "updated_at", None) != expected_updated_at:
                raise RuntimeError("record changed concurrently")
            transaction.set(reference, record.model_dump(mode="json"))

        await replace_in_transaction(transaction)
        return record

    async def list_all(self, *, limit: int | None = None) -> list[RecordT]:
        query = self._collection
        if limit is not None:
            query = query.limit(limit)
        snapshots = [snapshot async for snapshot in query.stream()]
        return [self.model_type.model_validate(item.to_dict()) for item in snapshots]


class MemoryRecordRepository(Generic[RecordT]):
    def __init__(self, model_type: type[RecordT]) -> None:
        self.model_type = model_type
        self._records: dict[str, RecordT] = {}
        self._lock = Lock()

    async def create(self, record: RecordT) -> RecordT:
        async with self._lock:
            if record.id in self._records:
                raise ValueError(f"record {record.id!r} already exists")
            self._records[record.id] = deepcopy(record)
        return deepcopy(record)

    async def create_or_get(self, record: RecordT) -> RecordT:
        async with self._lock:
            if record.id not in self._records:
                self._records[record.id] = deepcopy(record)
            return deepcopy(self._records[record.id])

    async def get(self, record_id: str) -> RecordT | None:
        async with self._lock:
            record = self._records.get(record_id)
            return deepcopy(record) if record is not None else None

    async def list_for_task(self, task_id: str) -> list[RecordT]:
        async with self._lock:
            records = [
                deepcopy(record)
                for record in self._records.values()
                if getattr(record, "task_id", None) == task_id
            ]
        records.sort(key=_record_sort_key, reverse=True)
        return records

    async def list_for_task_page(
        self, task_id: str, *, limit: int, before: str | None = None
    ) -> list[RecordT]:
        before_time = (
            datetime.fromisoformat(before.replace("Z", "+00:00")) if before is not None else None
        )
        async with self._lock:
            records = [
                deepcopy(record)
                for record in self._records.values()
                if getattr(record, "task_id", None) == task_id
                and (before_time is None or getattr(record, "occurred_at") < before_time)
            ]
        records.sort(key=_record_sort_key, reverse=True)
        return records[:limit]

    async def replace(self, record: RecordT, *, expected_updated_at: Any = None) -> RecordT:
        async with self._lock:
            if record.id not in self._records:
                raise KeyError(record.id)
            current = self._records[record.id]
            if (
                expected_updated_at is not None
                and getattr(current, "updated_at", None) != expected_updated_at
            ):
                raise RuntimeError("record changed concurrently")
            self._records[record.id] = deepcopy(record)
        return deepcopy(record)

    async def list_all(self, *, limit: int | None = None) -> list[RecordT]:
        async with self._lock:
            records = [deepcopy(record) for record in self._records.values()]
        if limit is not None:
            records = records[:limit]
        return records


class FirestoreImmutableRecordRepository(FirestoreRecordRepository[RecordT]):
    async def replace(self, record: RecordT, *, expected_updated_at: Any = None) -> RecordT:
        raise TypeError("immutable records cannot be replaced")


class MemoryImmutableRecordRepository(MemoryRecordRepository[RecordT]):
    async def replace(self, record: RecordT, *, expected_updated_at: Any = None) -> RecordT:
        raise TypeError("immutable records cannot be replaced")


@dataclass(frozen=True)
class WorkflowRepositories:
    executions: RecordRepository[Any]
    activity_events: ImmutableRecordRepository[Any]
    validation_reports: ImmutableRecordRepository[Any]
    security_reviews: ImmutableRecordRepository[Any]
    approvals: ImmutableRecordRepository[Any]
    pull_requests: RecordRepository[Any]


def create_memory_workflow_repositories() -> WorkflowRepositories:
    from app.schemas.approval import ApprovalRecord
    from app.schemas.execution import ActivityEvent, ExecutionRecord
    from app.schemas.pull_request import PullRequestRecord
    from app.schemas.security import SecurityReview
    from app.schemas.validation import ValidationReport

    return WorkflowRepositories(
        executions=MemoryRecordRepository(ExecutionRecord),
        activity_events=MemoryImmutableRecordRepository(ActivityEvent),
        validation_reports=MemoryImmutableRecordRepository(ValidationReport),
        security_reviews=MemoryImmutableRecordRepository(SecurityReview),
        approvals=MemoryImmutableRecordRepository(ApprovalRecord),
        pull_requests=MemoryRecordRepository(PullRequestRecord),
    )


def create_firestore_workflow_repositories(client: Any) -> WorkflowRepositories:
    from app.schemas.approval import ApprovalRecord
    from app.schemas.execution import ActivityEvent, ExecutionRecord
    from app.schemas.pull_request import PullRequestRecord
    from app.schemas.security import SecurityReview
    from app.schemas.validation import ValidationReport

    return WorkflowRepositories(
        executions=FirestoreRecordRepository(client, "executions", ExecutionRecord),
        activity_events=FirestoreImmutableRecordRepository(
            client, "activity_events", ActivityEvent
        ),
        validation_reports=FirestoreImmutableRecordRepository(
            client, "validation_reports", ValidationReport
        ),
        security_reviews=FirestoreImmutableRecordRepository(
            client, "security_reviews", SecurityReview
        ),
        approvals=FirestoreImmutableRecordRepository(client, "approvals", ApprovalRecord),
        pull_requests=FirestoreRecordRepository(client, "pull_requests", PullRequestRecord),
    )


__all__ = [
    "FirestoreRecordRepository",
    "FirestoreImmutableRecordRepository",
    "ImmutableRecordRepository",
    "MemoryRecordRepository",
    "MemoryImmutableRecordRepository",
    "RecordRepository",
    "WorkflowRepositories",
    "create_firestore_workflow_repositories",
    "create_memory_workflow_repositories",
]
