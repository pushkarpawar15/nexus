from __future__ import annotations

import re

import httpx

from app.core.config import RepositoryTarget, Settings


class GitHubOperationError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        token = settings.github_token.get_secret_value() if settings.github_token else None
        if not token:
            raise GitHubOperationError("GitHub token is not configured.")
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=httpx.Timeout(20.0),
            follow_redirects=False,
        )

    async def verify_target(self, target: RepositoryTarget) -> dict:
        response = await self.client.get(f"/repos/{target.owner}/{target.repo}")
        if response.status_code != 200:
            raise GitHubOperationError("GitHub repository could not be verified.")
        data = response.json()
        expected = f"{target.owner}/{target.repo}".casefold()
        if str(data.get("full_name", "")).casefold() != expected or not data.get(
            "permissions", {}
        ).get("push", False):
            raise GitHubOperationError("GitHub repository identity or write permission is invalid.")
        branch = await self.client.get(
            f"/repos/{target.owner}/{target.repo}/branches/{target.base_branch}"
        )
        if branch.status_code != 200 or not re.fullmatch(
            r"[0-9a-f]{40}", str(branch.json().get("commit", {}).get("sha", ""))
        ):
            raise GitHubOperationError("Configured base branch could not be verified.")
        return data

    async def find_pull_request(self, target: RepositoryTarget, branch: str) -> dict | None:
        response = await self.client.get(
            f"/repos/{target.owner}/{target.repo}/pulls",
            params={
                "head": f"{target.owner}:{branch}",
                "base": target.base_branch,
                "state": "all",
                "per_page": 100,
            },
        )
        if response.status_code != 200 or not isinstance(response.json(), list):
            raise GitHubOperationError("GitHub pull requests could not be listed.")
        pull_requests = response.json()
        # A follow-up patch reuses the pull request branch, so the same head
        # branch can carry an older, closed pull request as well; publication
        # must attach to the open one when it exists.
        open_pull_requests = [pr for pr in pull_requests if pr.get("state") == "open"]
        candidates = open_pull_requests or pull_requests
        return candidates[0] if candidates else None

    async def get_pull_request(self, target: RepositoryTarget, number: int) -> dict:
        response = await self.client.get(f"/repos/{target.owner}/{target.repo}/pulls/{number}")
        if response.status_code != 200 or not isinstance(response.json(), dict):
            raise GitHubOperationError("GitHub pull request status could not be confirmed.")
        return response.json()

    async def create_pull_request(
        self, target: RepositoryTarget, title: str, branch: str, body: str | None = None
    ) -> dict:
        response = await self.client.post(
            f"/repos/{target.owner}/{target.repo}/pulls",
            json={
                "title": title[:250],
                "head": branch,
                "base": target.base_branch,
                "body": body or "Created by the NEXUS engineering workflow.",
            },
        )
        if response.status_code not in {200, 201}:
            raise GitHubOperationError("GitHub did not confirm pull request creation.")
        return response.json()

    async def close(self) -> None:
        await self.client.aclose()


__all__ = ["GitHubClient", "GitHubOperationError"]
