from __future__ import annotations

import re

import httpx
from pydantic import SecretStr

from app.core.config import RepositoryTarget


class GitHubPATError(RuntimeError):
    """A supplied GitHub token cannot access the requested repository."""


class GitHubPATIntegration:
    """Process-local fine-grained PAT connection for one selected repository."""

    def __init__(self) -> None:
        self._token: SecretStr | None = None
        self._target: RepositoryTarget | None = None
        self._account_login: str | None = None

    @property
    def connected(self) -> bool:
        return self._token is not None and self._target is not None

    @property
    def token(self) -> SecretStr | None:
        return self._token

    @property
    def target(self) -> RepositoryTarget | None:
        return self._target

    async def connect(self, token: str, owner: str, repo: str) -> tuple[RepositoryTarget, str]:
        # Validate owner/repo before using them in a GitHub API path.
        RepositoryTarget(owner=owner, repo=repo)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            headers=headers,
            timeout=20,
            follow_redirects=False,
        ) as client:
            user_response = await client.get("/user")
            repository_response = await client.get(f"/repos/{owner}/{repo}")

        if user_response.status_code != 200 or repository_response.status_code != 200:
            raise GitHubPATError("GitHub rejected the token or it cannot access this repository.")
        user_data = user_response.json()
        repository_data = repository_response.json()
        if repository_data.get("full_name", "").casefold() != f"{owner}/{repo}".casefold():
            raise GitHubPATError("The GitHub repository identity did not match the request.")
        if not repository_data.get("permissions", {}).get("push", False):
            raise GitHubPATError(
                "This token needs write access to repository contents and pull requests."
            )

        repository_id = repository_data.get("id")
        default_branch = repository_data.get("default_branch") or "main"
        login = user_data.get("login")
        if (
            not isinstance(repository_id, int)
            or not isinstance(login, str)
            or not re.fullmatch(r"[A-Za-z0-9._/-]{1,250}", default_branch)
        ):
            raise GitHubPATError("GitHub returned incomplete repository information.")

        target = RepositoryTarget(
            owner=owner,
            repo=repo,
            label=f"{owner}/{repo}",
            base_branch=default_branch,
            github_repository_id=repository_id,
            github_private=bool(repository_data.get("private", False)),
        )
        self._token = SecretStr(token)
        self._target = target
        self._account_login = login
        return target, login

    def status(self) -> dict[str, object]:
        target = self._target
        return {
            "configured": True,
            "connected": self.connected,
            "account_login": self._account_login,
            "mode": "pat" if self.connected else None,
            "repository": target.label if target else None,
            "repository_info": (
                {
                    "key": f"github-{target.github_repository_id}",
                    "label": target.label or f"{target.owner}/{target.repo}",
                    "owner": target.owner,
                    "repo": target.repo,
                    "base_branch": target.base_branch,
                    "private": bool(target.github_private),
                }
                if target
                else None
            ),
            "pat_supported": True,
        }

    def disconnect(self) -> None:
        self._token = None
        self._target = None
        self._account_login = None


__all__ = ["GitHubPATError", "GitHubPATIntegration"]
