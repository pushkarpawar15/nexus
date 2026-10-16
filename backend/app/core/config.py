from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "testing", "production"]
StorageMode = Literal["firestore", "memory"]
ExecutionBackend = Literal["local", "cloud_run_jobs"]
CodingAgentRuntime = Literal["antigravity", "gemini"]
_REPOSITORY_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,149}$")
_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_EMULATOR_HOST = re.compile(r"^(?:localhost|[A-Za-z0-9.-]+)(?::[0-9]{1,5})?$")
_GITHUB_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


class ConfigurationError(RuntimeError):
    """Raised when NEXUS cannot start with the supplied settings."""


class RepositoryTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    owner: str = Field(min_length=1, max_length=100)
    repo: str = Field(min_length=1, max_length=100)
    label: str | None = Field(default=None, min_length=1, max_length=150)
    base_branch: str = Field(default="main", min_length=1, max_length=250)
    source_path: Path | None = None
    github_repository_id: int | None = Field(default=None, ge=1)
    github_installation_id: int | None = Field(default=None, ge=1)
    github_private: bool | None = None
    # Accept legacy Firestore documents, but never expose or use this profile.
    validation_profile: str | None = Field(default=None, exclude=True)

    @field_validator("owner", "repo")
    @classmethod
    def validate_github_component(cls, value: str) -> str:
        if not _GITHUB_COMPONENT.fullmatch(value):
            raise ValueError("must be a valid GitHub owner or repository name")
        return value

    @field_validator("base_branch")
    @classmethod
    def validate_base_branch(cls, value: str) -> str:
        if (
            value.startswith("-")
            or value.startswith("/")
            or value.endswith("/")
            or ".." in value
            or "\\" in value
            or "@{" in value
            or not re.fullmatch(r"[A-Za-z0-9._/-]{1,250}", value)
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("base_branch must be a safe Git branch name")
        return value

    @field_validator("source_path")
    @classmethod
    def require_absolute_source_path(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("source_path must be an absolute path")
        return value


class Settings(BaseSettings):
    """Validated runtime settings. Storage adapter selection is always explicit."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        env_ignore_empty=True,
        extra="ignore",
        populate_by_name=True,
    )

    environment: Environment = Field(
        default="development", validation_alias=AliasChoices("NEXUS_ENV", "environment")
    )
    storage: StorageMode | None = Field(
        default=None, validation_alias=AliasChoices("NEXUS_STORAGE", "storage")
    )
    google_cloud_project: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GOOGLE_CLOUD_PROJECT", "google_cloud_project"),
    )
    google_cloud_location: str = Field(
        default="asia-south1",
        validation_alias=AliasChoices("GOOGLE_CLOUD_LOCATION", "google_cloud_location"),
    )
    gemini_model: str = Field(
        default="gemini-3.8-flash",
        min_length=1,
        validation_alias=AliasChoices("GEMINI_MODEL", "gemini_model"),
    )
    gemini_analysis_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GEMINI_ANALYSIS_MODEL", "gemini_analysis_model"),
    )
    gemini_coding_model: str = Field(
        default="gemini-3.8-flash",
        min_length=1,
        validation_alias=AliasChoices("GEMINI_CODING_MODEL", "gemini_coding_model"),
    )
    coding_agent_runtime: CodingAgentRuntime = Field(
        default="antigravity",
        validation_alias=AliasChoices("NEXUS_CODING_AGENT", "coding_agent_runtime"),
    )
    coding_model_location: str = Field(
        default="global",
        min_length=1,
        validation_alias=AliasChoices("CODING_MODEL_LOCATION", "coding_model_location"),
    )
    gemini_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("GEMINI_API_KEY", "gemini_api_key"),
        repr=False,
    )
    firestore_database: str = Field(
        default="(default)",
        validation_alias=AliasChoices("FIRESTORE_DATABASE", "firestore_database"),
    )
    firestore_emulator_host: str | None = Field(
        default=None,
        validation_alias=AliasChoices("FIRESTORE_EMULATOR_HOST", "firestore_emulator_host"),
    )
    firebase_project_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("FIREBASE_PROJECT_ID", "firebase_project_id"),
    )
    firebase_auth_emulator_host: str | None = Field(
        default=None,
        validation_alias=AliasChoices("FIREBASE_AUTH_EMULATOR_HOST", "firebase_auth_emulator_host"),
    )
    cors_origins_raw: str = Field(
        default="localhost", validation_alias=AliasChoices("CORS_ORIGINS", "cors_origins_raw")
    )
    approved_repositories: dict[str, RepositoryTarget] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("APPROVED_REPOSITORIES", "approved_repositories"),
    )
    ai_permissions_raw: str = Field(
        default="", validation_alias=AliasChoices("NEXUS_AI_PERMISSIONS", "ai_permissions_raw")
    )
    coding_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("NEXUS_CODING_ENABLED", "coding_enabled"),
    )
    validation_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("NEXUS_VALIDATION_ENABLED", "validation_enabled"),
    )
    execution_backend: ExecutionBackend = Field(
        default="local",
        validation_alias=AliasChoices("NEXUS_EXECUTION_BACKEND", "execution_backend"),
    )
    cloud_run_job_name: str = Field(
        default="nexus-execution-worker",
        validation_alias=AliasChoices("CLOUD_RUN_EXECUTION_JOB", "cloud_run_job_name"),
    )
    cloud_run_region: str = Field(
        default="asia-south1",
        validation_alias=AliasChoices("CLOUD_RUN_EXECUTION_REGION", "cloud_run_region"),
    )
    cloud_run_execution_timeout_seconds: int = Field(
        default=7200,
        ge=60,
        le=604800,
        validation_alias=AliasChoices(
            "CLOUD_RUN_EXECUTION_TIMEOUT_SECONDS", "cloud_run_execution_timeout_seconds"
        ),
    )
    workspace_root: Path | None = Field(
        default=None, validation_alias=AliasChoices("WORKSPACE_ROOT", "workspace_root")
    )
    attachments_root: Path = Field(
        default=Path("/tmp/nexus-attachments"),
        validation_alias=AliasChoices("ATTACHMENTS_ROOT", "attachments_root"),
    )
    attachments_bucket: str | None = Field(
        default=None, validation_alias=AliasChoices("ATTACHMENTS_BUCKET", "attachments_bucket")
    )
    coding_model_timeout_seconds: int = Field(
        default=180,
        ge=5,
        le=180,
        validation_alias=AliasChoices(
            "CODING_MODEL_TIMEOUT_SECONDS", "coding_model_timeout_seconds"
        ),
    )
    validation_agent_timeout_seconds: int = Field(
        default=3600,
        ge=60,
        le=5400,
        validation_alias=AliasChoices(
            "NEXUS_VALIDATION_TIMEOUT_SECONDS", "validation_agent_timeout_seconds"
        ),
    )
    github_target_owner: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GITHUB_TARGET_OWNER", "github_target_owner"),
    )
    github_target_repo: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GITHUB_TARGET_REPO", "github_target_repo"),
    )
    github_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("GITHUB_TOKEN", "github_token"),
        repr=False,
    )
    github_app_slug: str | None = Field(
        default=None, validation_alias=AliasChoices("GITHUB_APP_SLUG", "github_app_slug")
    )
    github_app_id: str | None = Field(
        default=None, validation_alias=AliasChoices("GITHUB_APP_ID", "github_app_id")
    )
    github_app_private_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("GITHUB_APP_PRIVATE_KEY", "github_app_private_key"),
        repr=False,
    )
    github_app_state_secret: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("GITHUB_APP_STATE_SECRET", "github_app_state_secret"),
        repr=False,
    )
    github_app_webhook_secret: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("GITHUB_APP_WEBHOOK_SECRET", "github_app_webhook_secret"),
        repr=False,
    )
    frontend_url: str = Field(
        default="http://localhost:5173",
        validation_alias=AliasChoices("FRONTEND_URL", "frontend_url"),
    )

    @field_validator("google_cloud_project", "firebase_project_id")
    @classmethod
    def normalize_project_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if not _PROJECT_ID.fullmatch(value):
            raise ValueError("must be a valid Google Cloud project ID")
        return value

    @field_validator("firestore_emulator_host", "firebase_auth_emulator_host")
    @classmethod
    def validate_emulator_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or not _EMULATOR_HOST.fullmatch(value):
            raise ValueError("must be a hostname with an optional port")
        port = value.rpartition(":")[2] if ":" in value else ""
        if port and not 1 <= int(port) <= 65535:
            raise ValueError("emulator port must be between 1 and 65535")
        return value

    @field_validator("approved_repositories")
    @classmethod
    def validate_repository_keys(
        cls, value: dict[str, RepositoryTarget]
    ) -> dict[str, RepositoryTarget]:
        if len(value) > 500:
            raise ValueError("APPROVED_REPOSITORIES supports at most 500 targets")
        if any(not _REPOSITORY_KEY.fullmatch(key) for key in value):
            raise ValueError("repository keys must use lowercase letters, digits, '-' or '_'")
        return value

    @field_validator("ai_permissions_raw")
    @classmethod
    def validate_ai_permissions(cls, value: str) -> str:
        allowed = {"ai_execute", "ai_validate", "security_review", "medium_skip_approval"}
        permissions = {item.strip() for item in value.split(",") if item.strip()}
        unknown = permissions - allowed
        if unknown:
            raise ValueError(f"unknown NEXUS_AI_PERMISSIONS: {', '.join(sorted(unknown))}")
        return value

    @field_validator("workspace_root")
    @classmethod
    def require_absolute_workspace_root(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("WORKSPACE_ROOT must be an absolute path")
        return value

    @field_validator("attachments_root")
    @classmethod
    def require_absolute_attachments_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("ATTACHMENTS_ROOT must be an absolute path")
        return value

    @property
    def approved_repository_keys(self) -> frozenset[str]:
        return frozenset(self.approved_repositories)

    @property
    def cors_origins(self) -> tuple[str, ...]:
        return tuple(
            origin.strip() for origin in self.cors_origins_raw.split(",") if origin.strip()
        )

    @property
    def ai_permissions(self) -> frozenset[str]:
        return frozenset(
            permission.strip()
            for permission in self.ai_permissions_raw.split(",")
            if permission.strip()
        )


def validate_settings(settings: Settings) -> None:
    if settings.storage not in {"firestore", "memory"}:
        raise ConfigurationError("NEXUS_STORAGE must explicitly be 'firestore' or 'memory'.")
    if settings.environment == "production" and settings.storage != "firestore":
        raise ConfigurationError("Production requires NEXUS_STORAGE=firestore.")
    if settings.storage == "memory" and settings.environment != "testing":
        raise ConfigurationError("In-memory storage is available only when NEXUS_ENV=testing.")
    if (
        settings.storage == "firestore"
        and not settings.google_cloud_project
        and not settings.firestore_emulator_host
    ):
        raise ConfigurationError("GOOGLE_CLOUD_PROJECT is required when using Firestore.")
    if settings.environment == "production":
        if not settings.google_cloud_project:
            raise ConfigurationError("GOOGLE_CLOUD_PROJECT is required in production.")
        if not settings.firebase_project_id:
            raise ConfigurationError("FIREBASE_PROJECT_ID is required in production.")
        if not settings.approved_repositories and not (
            settings.github_app_slug
            and settings.github_app_id
            and settings.github_app_private_key
            and settings.github_app_state_secret
        ):
            raise ConfigurationError(
                "Configure an approved repository or a complete GitHub App integration."
            )
        if settings.firestore_emulator_host or settings.firebase_auth_emulator_host:
            raise ConfigurationError("Emulators must not be enabled in production.")
        if settings.coding_enabled != settings.validation_enabled:
            raise ConfigurationError("Coding and validation must be enabled or disabled together.")
        if settings.coding_enabled and settings.execution_backend != "cloud_run_jobs":
            raise ConfigurationError(
                "Production execution requires NEXUS_EXECUTION_BACKEND=cloud_run_jobs."
            )
        if settings.coding_enabled and settings.execution_backend == "cloud_run_jobs":
            if not settings.cloud_run_job_name or not settings.cloud_run_region:
                raise ConfigurationError("Cloud Run execution job and region are required.")
            if not settings.github_app_slug and not (
                settings.github_token and settings.approved_repositories
            ):
                raise ConfigurationError(
                    "Cloud execution requires a GitHub App installation or a server-configured "
                    "GITHUB_TOKEN with explicit APPROVED_REPOSITORIES."
                )
        if not settings.attachments_bucket:
            raise ConfigurationError("ATTACHMENTS_BUCKET is required in production.")
    if (
        settings.coding_enabled
        and settings.execution_backend == "local"
        and not settings.workspace_root
    ):
        raise ConfigurationError("WORKSPACE_ROOT is required when local execution is enabled.")
    if settings.workspace_root is not None:
        workspace = settings.workspace_root.resolve()
        # In the source checkout this module is four levels below the repo root,
        # while the container installs it under /app/app/core. parents[3] is
        # therefore the filesystem root (/) in Cloud Run and would reject every
        # valid workspace beneath /tmp. Protect the installed application root.
        protected_paths = [Path(__file__).resolve().parents[2]]
        protected_paths.extend(
            target.source_path.resolve()
            for target in settings.approved_repositories.values()
            if target.source_path is not None
        )
        for protected in protected_paths:
            overlaps = (
                workspace == protected
                or workspace in protected.parents
                or protected in workspace.parents
            )
            if overlaps:
                raise ConfigurationError(
                    "WORKSPACE_ROOT must be separate from the NEXUS and target checkouts."
                )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    validate_settings(settings)
    return settings
