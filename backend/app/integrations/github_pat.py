from __future__ import annotations

import re

import httpx

from app.core.config import RepositoryTarget


class GitHubPATError(RuntimeError):
    """A supplied GitHub token cannot access the requested repository."""


class GitHubPATIntegration:
    """Validates fine-grained PAT access without retaining the supplied token."""

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
        return target, login


__all__ = ["GitHubPATError", "GitHubPATIntegration"]
