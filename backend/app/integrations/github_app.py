from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from typing import Any

import httpx

from app.core.config import RepositoryTarget, Settings


class GitHubAppError(RuntimeError):
    pass


class GitHubAppIntegration:
    """GitHub App installation flow and short-lived installation tokens."""

    def __init__(self, settings: Settings, firestore_client: Any | None = None) -> None:
        self.settings = settings
        self.firestore = firestore_client
        self._memory_installation_id: int | None = None
        self._repository_cache: list[RepositoryTarget] = []
        self._repository_cache_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.github_app_slug
            and self.settings.github_app_id
            and self.settings.github_app_private_key
            and self.settings.github_app_state_secret
        )

    def install_url(self, user_id: str) -> str:
        if not self.configured:
            raise GitHubAppError("GitHub App configuration is incomplete.")
        state = self._sign_state(user_id)
        return (
            f"https://github.com/apps/{self.settings.github_app_slug}/installations/new"
            f"?state={state}"
        )

    def _sign_state(self, user_id: str) -> str:
        payload = json.dumps(
            {"uid": user_id, "exp": int(time.time()) + 600}, separators=(",", ":")
        ).encode()
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
        secret = self.settings.github_app_state_secret.get_secret_value().encode()
        signature = hmac.new(secret, encoded, hashlib.sha256).digest()
        return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"

    def _verify_state(self, state: str) -> str:
        try:
            encoded, signature = state.split(".", 1)
            secret = self.settings.github_app_state_secret.get_secret_value().encode()
            expected = (
                base64.urlsafe_b64encode(
                    hmac.new(secret, encoded.encode(), hashlib.sha256).digest()
                )
                .rstrip(b"=")
                .decode()
            )
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            if int(payload["exp"]) < int(time.time()) or not isinstance(payload["uid"], str):
                raise ValueError
            return payload["uid"]
        except Exception as exc:
            raise GitHubAppError("GitHub installation state is invalid or expired.") from exc

    def _app_jwt(self) -> str:
        if not self.configured:
            raise GitHubAppError("GitHub App configuration is incomplete.")
        try:
            import jwt

            now = int(time.time())
            private_key = self.settings.github_app_private_key.get_secret_value().replace(
                "\\n", "\n"
            )
            token = jwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": self.settings.github_app_id},
                private_key,
                algorithm="RS256",
            )
            return token.decode() if isinstance(token, bytes) else token
        except Exception as exc:
            raise GitHubAppError("GitHub App credentials could not sign a request.") from exc

    async def save_installation(self, installation_id: int, state: str) -> str:
        uid = self._verify_state(state)
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {self._app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20,
            follow_redirects=False,
        ) as client:
            response = await client.get(f"/app/installations/{installation_id}")
        if response.status_code != 200 or response.json().get("app_id") != int(
            self.settings.github_app_id
        ):
            raise GitHubAppError("This GitHub installation does not belong to the configured app.")
        info = response.json()
        record = {
            "installation_id": installation_id,
            "account_login": info.get("account", {}).get("login", "GitHub account"),
            "account_type": info.get("account", {}).get("type", "Organization"),
            "connected_by": uid,
            "updated_at": int(time.time()),
        }
        if self.firestore is not None:
            await self.firestore.collection("nexus_integrations").document("github").set(record)
        else:
            self._memory_installation_id = installation_id
        return record["account_login"]

    async def status(self) -> dict[str, Any]:
        if not self.configured:
            return {"configured": False, "connected": False, "account_login": None}
        record = await self._installation_record()
        return {
            "configured": True,
            "connected": record is not None,
            "account_login": record.get("account_login") if record else None,
            "installation_id": record.get("installation_id") if record else None,
        }

    async def disconnect(self) -> None:
        if self.firestore is not None:
            await self.firestore.collection("nexus_integrations").document("github").delete()
        self._memory_installation_id = None
        self._repository_cache = []
        self._repository_cache_at = 0.0

    def verify_webhook(self, body: bytes, signature: str | None) -> bool:
        secret = self.settings.github_app_webhook_secret
        if not secret or not signature or not signature.startswith("sha256="):
            return False
        expected = (
            "sha256="
            + hmac.new(secret.get_secret_value().encode(), body, hashlib.sha256).hexdigest()
        )
        return hmac.compare_digest(signature, expected)

    async def handle_webhook(self, event: str, payload: dict[str, Any]) -> None:
        if event == "installation_repositories":
            self._repository_cache_at = 0.0
        elif event == "installation" and payload.get("action") == "deleted":
            record = await self._installation_record()
            incoming = payload.get("installation", {}).get("id")
            if record and int(record.get("installation_id", 0)) == int(incoming or 0):
                await self.disconnect()

    async def repositories(self) -> list[RepositoryTarget]:
        if time.monotonic() - self._repository_cache_at < 60:
            return list(self._repository_cache)
        record = await self._installation_record()
        if record is None:
            return []
        token = await self.installation_token(int(record["installation_id"]))
        repositories: list[RepositoryTarget] = []
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            headers=self._headers(token),
            timeout=20,
            follow_redirects=False,
        ) as client:
            page = 1
            while page <= 5:
                response = await client.get(
                    "/installation/repositories", params={"per_page": 100, "page": page}
                )
                if response.status_code != 200:
                    raise GitHubAppError("GitHub repositories could not be listed.")
                data = response.json()
                for item in data.get("repositories", []):
                    repository_id = int(item["id"])
                    owner = item.get("owner", {}).get("login", "")
                    name = item.get("name", "")
                    valid_component = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}"
                    if not re.fullmatch(valid_component, owner) or not re.fullmatch(
                        valid_component, name
                    ):
                        continue
                    repositories.append(
                        RepositoryTarget(
                            owner=owner,
                            repo=name,
                            label=item.get("full_name", f"{owner}/{name}"),
                            base_branch=item.get("default_branch") or "main",
                            github_repository_id=repository_id,
                            github_installation_id=int(record["installation_id"]),
                            github_private=bool(item.get("private", False)),
                        )
                    )
                if len(data.get("repositories", [])) < 100:
                    break
                page += 1
        self._repository_cache = repositories
        self._repository_cache_at = time.monotonic()
        return list(repositories)

    async def installation_token(self, installation_id: int) -> str:
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {self._app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=20,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                f"/app/installations/{installation_id}/access_tokens",
                json={
                    "permissions": {"contents": "write", "pull_requests": "write"},
                },
            )
        if response.status_code != 201 or not response.json().get("token"):
            raise GitHubAppError(
                "GitHub could not issue a repository access token. Check App permissions."
            )
        return str(response.json()["token"])

    async def _installation_record(self) -> dict[str, Any] | None:
        if self.firestore is not None:
            snapshot = await (
                self.firestore.collection("nexus_integrations").document("github").get()
            )
            return snapshot.to_dict() if snapshot.exists else None
        if self._memory_installation_id is not None:
            return {"installation_id": self._memory_installation_id, "account_login": "GitHub"}
        return None

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }


__all__ = ["GitHubAppError", "GitHubAppIntegration"]
