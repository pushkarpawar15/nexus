from __future__ import annotations

from asyncio import Lock
from copy import deepcopy
from typing import Any, Protocol

from app.schemas.skills import SkillRecord


class SkillRepository(Protocol):
    async def create(self, skill: SkillRecord) -> SkillRecord: ...

    async def get(self, skill_id: str) -> SkillRecord | None: ...

    async def list_all(self) -> list[SkillRecord]: ...

    async def replace(self, skill: SkillRecord, *, expected_updated_at: Any) -> SkillRecord: ...

    async def delete(self, skill_id: str, *, expected_updated_at: Any) -> bool: ...


class MemorySkillRepository:
    def __init__(self) -> None:
        self._skills: dict[str, SkillRecord] = {}
        self._lock = Lock()

    async def create(self, skill: SkillRecord) -> SkillRecord:
        async with self._lock:
            if skill.id in self._skills:
                raise ValueError("skill already exists")
            self._skills[skill.id] = deepcopy(skill)
            return deepcopy(skill)

    async def get(self, skill_id: str) -> SkillRecord | None:
        async with self._lock:
            skill = self._skills.get(skill_id)
            return deepcopy(skill) if skill is not None else None

    async def list_all(self) -> list[SkillRecord]:
        async with self._lock:
            return deepcopy(list(self._skills.values()))

    async def replace(self, skill: SkillRecord, *, expected_updated_at: Any) -> SkillRecord:
        async with self._lock:
            current = self._skills.get(skill.id)
            if current is None or current.updated_at != expected_updated_at:
                raise RuntimeError("skill changed concurrently")
            self._skills[skill.id] = deepcopy(skill)
            return deepcopy(skill)

    async def delete(self, skill_id: str, *, expected_updated_at: Any) -> bool:
        async with self._lock:
            current = self._skills.get(skill_id)
            if current is None:
                return False
            if current.updated_at != expected_updated_at:
                raise RuntimeError("skill changed concurrently")
            del self._skills[skill_id]
            return True


class FirestoreSkillRepository:
    def __init__(self, client: Any) -> None:
        self._client = client
        self._collection = client.collection("skills")

    async def create(self, skill: SkillRecord) -> SkillRecord:
        await self._collection.document(skill.id).create(skill.model_dump(mode="json"))
        return skill

    async def get(self, skill_id: str) -> SkillRecord | None:
        snapshot = await self._collection.document(skill_id).get()
        return SkillRecord.model_validate(snapshot.to_dict()) if snapshot.exists else None

    async def list_all(self) -> list[SkillRecord]:
        snapshots = [snapshot async for snapshot in self._collection.stream()]
        return [SkillRecord.model_validate(snapshot.to_dict()) for snapshot in snapshots]

    async def replace(self, skill: SkillRecord, *, expected_updated_at: Any) -> SkillRecord:
        from google.cloud.firestore_v1.async_transaction import async_transactional

        reference = self._collection.document(skill.id)
        transaction = self._client.transaction()

        @async_transactional
        async def replace_in_transaction(transaction):
            snapshot = await reference.get(transaction=transaction)
            if not snapshot.exists:
                raise KeyError(skill.id)
            current = SkillRecord.model_validate(snapshot.to_dict())
            if current.updated_at != expected_updated_at:
                raise RuntimeError("skill changed concurrently")
            transaction.set(reference, skill.model_dump(mode="json"))

        await replace_in_transaction(transaction)
        return skill

    async def delete(self, skill_id: str, *, expected_updated_at: Any) -> bool:
        from google.cloud.firestore_v1.async_transaction import async_transactional

        reference = self._collection.document(skill_id)
        transaction = self._client.transaction()

        @async_transactional
        async def delete_in_transaction(transaction):
            snapshot = await reference.get(transaction=transaction)
            if not snapshot.exists:
                return False
            current = SkillRecord.model_validate(snapshot.to_dict())
            if current.updated_at != expected_updated_at:
                raise RuntimeError("skill changed concurrently")
            transaction.delete(reference)
            return True

        return await delete_in_transaction(transaction)


__all__ = ["FirestoreSkillRepository", "MemorySkillRepository", "SkillRepository"]
