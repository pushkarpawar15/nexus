from datetime import datetime, timezone

from app.cloud_worker import _apply_connection_validation_profile
from app.core.config import RepositoryTarget, Settings, ValidationProfile
from app.repositories.github_connections import GitHubRepositoryConnection


def test_cloud_worker_keeps_profile_when_github_app_target_is_preferred() -> None:
    repository_key = "github-1411750689"
    app_target = RepositoryTarget(
        owner="pushkarpawar15",
        repo="nexus",
        github_repository_id=1411750689,
        github_installation_id=1234,
    )
    connection_target = RepositoryTarget(
        owner="pushkarpawar15",
        repo="nexus",
        github_repository_id=1411750689,
        validation_profile=ValidationProfile.NEXUS_MONOREPO,
    )
    now = datetime.now(timezone.utc)
    connection = GitHubRepositoryConnection(
        user_id="user-1",
        account_login="pushkarpawar15",
        target=connection_target,
        secret_name="nexus-github-test",
        created_at=now,
        updated_at=now,
    )
    settings = Settings(
        _env_file=None,
        environment="testing",
        storage="memory",
        approved_repositories={repository_key: app_target},
    )

    updated = _apply_connection_validation_profile(settings, repository_key, connection)

    target = updated.approved_repositories[repository_key]
    assert target.validation_profile == ValidationProfile.NEXUS_MONOREPO
    assert target.github_installation_id == app_target.github_installation_id


def test_cloud_worker_clears_app_profile_when_connection_has_no_profile() -> None:
    repository_key = "github-1411750689"
    now = datetime.now(timezone.utc)
    connection = GitHubRepositoryConnection(
        user_id="user-1",
        account_login="pushkarpawar15",
        target=RepositoryTarget(
            owner="pushkarpawar15",
            repo="nexus",
            github_repository_id=1411750689,
        ),
        secret_name="nexus-github-test",
        created_at=now,
        updated_at=now,
    )
    settings = Settings(
        _env_file=None,
        environment="testing",
        storage="memory",
        approved_repositories={
            repository_key: RepositoryTarget(
                owner="pushkarpawar15",
                repo="nexus",
                github_repository_id=1411750689,
                validation_profile=ValidationProfile.NEXUS_MONOREPO,
            )
        },
    )

    updated = _apply_connection_validation_profile(settings, repository_key, connection)

    assert updated.approved_repositories[repository_key].validation_profile is None
