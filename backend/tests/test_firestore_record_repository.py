from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from pydantic import BaseModel

from app.repositories.records import FirestoreRecordRepository


class ActivityRecord(BaseModel):
    id: str
    task_id: str
    occurred_at: datetime


class Snapshot:
    def to_dict(self) -> dict[str, object]:
        return {
            "id": "event-1",
            "task_id": "task-1",
            "occurred_at": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        }


class Query:
    def __init__(self) -> None:
        self.ordering: tuple[str, str] | None = None
        self.page_limit: int | None = None
        self.filters: list[tuple[str, str, object]] = []

    def where(self, field: str, operator: str, value: object) -> Query:
        self.filters.append((field, operator, value))
        return self

    def order_by(self, field: str, *, direction: str) -> Query:
        self.ordering = (field, direction)
        return self

    def limit(self, count: int) -> Query:
        self.page_limit = count
        return self

    async def stream(self):
        yield Snapshot()


class Collection:
    def __init__(self) -> None:
        self.query = Query()

    def where(self, field: str, operator: str, value: object) -> Query:
        return self.query.where(field, operator, value)


class Client:
    def __init__(self) -> None:
        self.test_collection = Collection()

    def collection(self, _name: str) -> Collection:
        return self.test_collection


def test_firestore_record_page_uses_supported_descending_order() -> None:
    from google.cloud.firestore_v1.base_query import BaseQuery

    client = Client()
    repository = FirestoreRecordRepository(client, "activity_events", ActivityRecord)

    records = asyncio.run(
        repository.list_for_task_page(
            "task-1",
            limit=20,
            before="2026-02-01T00:00:00+00:00",
        )
    )

    assert len(records) == 1
    assert records[0].id == "event-1"
    assert client.test_collection.query.filters == [
        ("task_id", "==", "task-1"),
        ("occurred_at", "<", "2026-02-01T00:00:00+00:00"),
    ]
    assert client.test_collection.query.ordering == ("occurred_at", BaseQuery.DESCENDING)
    assert client.test_collection.query.page_limit == 20
