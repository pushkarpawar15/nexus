from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    task_repository: str | None
    approved_repository_keys: frozenset[str]
    google_cloud_project: str | None
    firestore_database: str
    firestore_tasks_collection: str
    firestore_events_collection: str

    @classmethod
    def from_env(cls) -> "Settings":
        raw_repositories = os.getenv("NEXUS_APPROVED_REPOSITORIES", "")
        repositories = frozenset(key.strip() for key in raw_repositories.split(",") if key.strip())
        repository = os.getenv("NEXUS_TASK_REPOSITORY")
        return cls(
            task_repository=repository.strip().lower() if repository else None,
            approved_repository_keys=repositories,
            google_cloud_project=os.getenv("GOOGLE_CLOUD_PROJECT") or None,
            firestore_database=os.getenv("FIRESTORE_DATABASE", "(default)"),
            firestore_tasks_collection=os.getenv("NEXUS_FIRESTORE_TASKS_COLLECTION", "nexus_tasks"),
            firestore_events_collection=os.getenv(
                "NEXUS_FIRESTORE_EVENTS_COLLECTION", "nexus_task_events"
            ),
        )


def validate_settings(settings: Settings) -> None:
    if len(settings.approved_repository_keys) > 30:
        raise ConfigurationError(
            "NEXUS_APPROVED_REPOSITORIES supports at most 30 keys for Firestore filtering."
        )
    if settings.task_repository not in {"firestore", "memory"}:
        raise ConfigurationError(
            "NEXUS_TASK_REPOSITORY must be explicitly set to 'firestore' or 'memory'; "
            "there is no implicit in-memory fallback."
        )
    if settings.task_repository == "firestore" and not settings.google_cloud_project:
        raise ConfigurationError(
            "GOOGLE_CLOUD_PROJECT is required when NEXUS_TASK_REPOSITORY=firestore."
        )
