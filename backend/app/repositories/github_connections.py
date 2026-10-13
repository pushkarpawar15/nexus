from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from app.core.config import RepositoryTarget


class GitHubRepositoryConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    account_login: str
    target: RepositoryTarget
    secret_name: str
    created_at: datetime
    updated_at: datetime

    @property
    def repository_key(self) -> str:
        return f"github-{self.target.github_repository_id}"


class GitHubConnectionRepository(Protocol):
    async def list_for_user(self, user_id: str) -> list[GitHubRepositoryConnection]: ...

    async def list_all(self) -> list[GitHubRepositoryConnection]: ...

    async def get(
        self, user_id: str, repository_key: str
    ) -> GitHubRepositoryConnection | None: ...

    async def put(
        self, connection: GitHubRepositoryConnection
    ) -> GitHubRepositoryConnection: ...

    async def delete(self, user_id: str, repository_key: str) -> bool: ...


def _document_id(user_id: str, repository_key: str) -> str:
    return hashlib.sha256(f"{user_id}:{repository_key}".encode()).hexdigest()


class MemoryGitHubConnectionRepository:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], GitHubRepositoryConnection] = {}

    async def list_for_user(self, user_id: str) -> list[GitHubRepositoryConnection]:
        return [
            connection.model_copy(deep=True)
            for (owner_id, _), connection in self._items.items()
            if owner_id == user_id
        ]

    async def list_all(self) -> list[GitHubRepositoryConnection]:
        return [item.model_copy(deep=True) for item in self._items.values()]

    async def get(
        self, user_id: str, repository_key: str
    ) -> GitHubRepositoryConnection | None:
        connection = self._items.get((user_id, repository_key))
        return connection.model_copy(deep=True) if connection is not None else None

    async def put(
        self, connection: GitHubRepositoryConnection
    ) -> GitHubRepositoryConnection:
        self._items[(connection.user_id, connection.repository_key)] = connection.model_copy(
            deep=True
        )
        return connection

    async def delete(self, user_id: str, repository_key: str) -> bool:
        return self._items.pop((user_id, repository_key), None) is not None


class FirestoreGitHubConnectionRepository:
    def __init__(self, client: Any) -> None:
        self._collection = client.collection("github_repository_connections")

    async def list_for_user(self, user_id: str) -> list[GitHubRepositoryConnection]:
        query = self._collection.where("user_id", "==", user_id)
        return [
            GitHubRepositoryConnection.model_validate(snapshot.to_dict())
            async for snapshot in query.stream()
        ]

    async def list_all(self) -> list[GitHubRepositoryConnection]:
        return [
            GitHubRepositoryConnection.model_validate(snapshot.to_dict())
            async for snapshot in self._collection.stream()
        ]

    async def get(
        self, user_id: str, repository_key: str
    ) -> GitHubRepositoryConnection | None:
        snapshot = await self._collection.document(_document_id(user_id, repository_key)).get()
        if not snapshot.exists:
            return None
        return GitHubRepositoryConnection.model_validate(snapshot.to_dict())

    async def put(
        self, connection: GitHubRepositoryConnection
    ) -> GitHubRepositoryConnection:
        await self._collection.document(
            _document_id(connection.user_id, connection.repository_key)
        ).set(connection.model_dump(mode="json"))
        return connection

    async def delete(self, user_id: str, repository_key: str) -> bool:
        reference = self._collection.document(_document_id(user_id, repository_key))
        snapshot = await reference.get()
        if not snapshot.exists:
            return False
        await reference.delete()
        return True


__all__ = [
    "FirestoreGitHubConnectionRepository",
    "GitHubConnectionRepository",
    "GitHubRepositoryConnection",
    "MemoryGitHubConnectionRepository",
]
