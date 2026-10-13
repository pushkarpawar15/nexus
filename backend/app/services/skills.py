from __future__ import annotations

import hashlib

from app.repositories.skills import SkillRepository
from app.schemas.skills import SkillRecord

MAX_EXECUTION_SKILLS = 20
MAX_TOTAL_SKILL_PROMPT_CHARS = 40_000


class SkillResolutionError(ValueError):
    pass


class SkillService:
    def __init__(self, repository: SkillRepository) -> None:
        self.repository = repository

    async def list_for_scope(self, repository_key: str | None) -> list[SkillRecord]:
        skills = await self.repository.list_all()
        return sorted(
            [
                skill
                for skill in skills
                if skill.scope == "global"
                or (repository_key is not None and skill.repository_key == repository_key)
            ],
            key=lambda skill: (skill.scope != "global", skill.name.casefold(), skill.id),
        )

    async def resolve_for_execution(self, repository_key: str) -> list[SkillRecord]:
        skills = [
            skill
            for skill in await self.repository.list_all()
            if skill.enabled
            and (
                skill.scope == "global"
                or (skill.scope == "repository" and skill.repository_key == repository_key)
            )
        ]
        skills.sort(key=lambda skill: (skill.scope != "global", skill.name.casefold(), skill.id))
        if len(skills) > MAX_EXECUTION_SKILLS:
            raise SkillResolutionError(
                f"Execution has {len(skills)} enabled matching skills; the maximum is "
                f"{MAX_EXECUTION_SKILLS}. Disable or remove skills before starting."
            )
        prompt_size = sum(
            len(
                f"{skill.name}\n{skill.description}\n{skill.instructions}"
            )
            for skill in skills
        )
        if prompt_size > MAX_TOTAL_SKILL_PROMPT_CHARS:
            raise SkillResolutionError(
                "Enabled matching skills exceed the combined 40000-character prompt limit. "
                "Shorten or disable skills before starting."
            )
        return skills


def skill_fingerprint(skill: SkillRecord) -> str:
    return hashlib.sha256(skill.instructions.encode("utf-8")).hexdigest()


__all__ = [
    "MAX_EXECUTION_SKILLS",
    "MAX_TOTAL_SKILL_PROMPT_CHARS",
    "SkillResolutionError",
    "SkillService",
    "skill_fingerprint",
]
