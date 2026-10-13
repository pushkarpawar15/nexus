from __future__ import annotations

import asyncio
from typing import Protocol

from pydantic import SecretStr

from app.core.config import Settings


class GitHubSecretStore(Protocol):
    async def put(self, secret_name: str, token: SecretStr) -> None: ...

    async def get(self, secret_name: str) -> SecretStr: ...

    async def delete(self, secret_name: str) -> None: ...


class MemoryGitHubSecretStore:
    def __init__(self) -> None:
        self._secrets: dict[str, SecretStr] = {}

    async def put(self, secret_name: str, token: SecretStr) -> None:
        self._secrets[secret_name] = token

    async def get(self, secret_name: str) -> SecretStr:
        token = self._secrets.get(secret_name)
        if token is None:
            raise RuntimeError("The repository credential is unavailable.")
        return token

    async def delete(self, secret_name: str) -> None:
        self._secrets.pop(secret_name, None)


class GoogleSecretManagerGitHubSecretStore:
    def __init__(self, settings: Settings) -> None:
        if not settings.google_cloud_project:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT is required for Secret Manager.")
        self._project_id = settings.google_cloud_project
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    async def put(self, secret_name: str, token: SecretStr) -> None:
        await asyncio.to_thread(self._put, secret_name, token.get_secret_value())

    def _put(self, secret_name: str, token: str) -> None:
        from google.api_core.exceptions import NotFound

        client = self._get_client()
        parent = f"projects/{self._project_id}"
        try:
            client.get_secret(request={"name": f"{parent}/secrets/{secret_name}"})
        except NotFound:
            client.create_secret(
                request={
                    "parent": parent,
                    "secret_id": secret_name,
                    "secret": {"replication": {"automatic": {}}},
                }
            )
        client.add_secret_version(
            request={
                "parent": f"{parent}/secrets/{secret_name}",
                "payload": {"data": token.encode()},
            }
        )

    async def get(self, secret_name: str) -> SecretStr:
        token = await asyncio.to_thread(self._get, secret_name)
        return SecretStr(token)

    def _get(self, secret_name: str) -> str:
        client = self._get_client()
        response = client.access_secret_version(
            request={"name": f"projects/{self._project_id}/secrets/{secret_name}/versions/latest"}
        )
        return response.payload.data.decode()

    async def delete(self, secret_name: str) -> None:
        await asyncio.to_thread(self._delete, secret_name)

    def _delete(self, secret_name: str) -> None:
        from google.api_core.exceptions import NotFound

        client = self._get_client()
        try:
            client.delete_secret(
                request={"name": f"projects/{self._project_id}/secrets/{secret_name}"}
            )
        except NotFound:
            return


__all__ = [
    "GoogleSecretManagerGitHubSecretStore",
    "GitHubSecretStore",
    "MemoryGitHubSecretStore",
]
