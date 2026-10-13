import asyncio
from datetime import datetime, timezone

import pytest

from app.repositories.skills import MemorySkillRepository
from app.schemas.skills import SkillRecord
from app.services.skills import SkillResolutionError, SkillService, skill_fingerprint


def _skill(
    skill_id: str,
    *,
    scope: str = "global",
    repository_key: str | None = None,
    enabled: bool = True,
    instructions: str = "Follow project conventions.",
) -> SkillRecord:
    now = datetime.now(timezone.utc)
    return SkillRecord(
        id=skill_id,
        name=skill_id,
        description="Test skill",
        instructions=instructions,
        scope=scope,
        repository_key=repository_key,
        enabled=enabled,
        version=1,
        created_at=now,
        updated_at=now,
        created_by="test",
        updated_by="test",
    )


def test_resolve_for_execution_selects_only_enabled_global_and_exact_repository_skills() -> None:
    async def exercise() -> None:
        repository = MemorySkillRepository()
        skills = [
            _skill("global"),
            _skill("matched", scope="repository", repository_key="sample"),
            _skill("other", scope="repository", repository_key="different"),
            _skill("disabled", enabled=False),
        ]
        for skill in skills:
            await repository.create(skill)

        resolved = await SkillService(repository).resolve_for_execution("sample")

        assert [skill.id for skill in resolved] == ["global", "matched"]
        assert skill_fingerprint(resolved[0]) == skill_fingerprint(skills[0])

    asyncio.run(exercise())


def test_resolve_for_execution_rejects_combined_prompt_overflow() -> None:
    async def exercise() -> None:
        repository = MemorySkillRepository()
        for index, content in enumerate(("a" * 20_000, "b" * 20_000, "c")):
            await repository.create(_skill(f"skill-{index}", instructions=content))

        with pytest.raises(
            SkillResolutionError,
            match="combined 40000-character prompt limit",
        ):
            await SkillService(repository).resolve_for_execution("sample")

    asyncio.run(exercise())


def test_resolve_for_execution_rejects_too_many_matching_skills() -> None:
    async def exercise() -> None:
        repository = MemorySkillRepository()
        for index in range(21):
            await repository.create(_skill(f"skill-{index}"))

        with pytest.raises(SkillResolutionError, match="maximum is 20"):
            await SkillService(repository).resolve_for_execution("sample")

    asyncio.run(exercise())
