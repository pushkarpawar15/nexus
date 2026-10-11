from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Any, Callable

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings, get_settings
from .errors import ErrorCode, NexusError


class UserRole(str, Enum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"


@dataclass(frozen=True)
class UserIdentity:
    uid: str
    role: UserRole
    email: str | None = None


bearer_scheme = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)
TokenVerifier = Callable[[str, Settings], dict[str, Any]]


@lru_cache(maxsize=8)
def _firebase_app(project_id: str) -> Any:
    try:
        import firebase_admin
    except ImportError as exc:  # pragma: no cover - exercised in minimal installations
        raise RuntimeError("firebase-admin is required to verify Firebase ID tokens") from exc

    app_name = f"nexus-auth-{project_id}"
    try:
        return firebase_admin.get_app(app_name)
    except ValueError:
        return firebase_admin.initialize_app(options={"projectId": project_id}, name=app_name)


def verify_firebase_id_token(token: str, settings: Settings) -> dict[str, Any]:
    if not settings.firebase_project_id:
        raise RuntimeError("Firebase project ID is not configured")
    if settings.firebase_auth_emulator_host:
        # Firebase Admin's emulator support is selected through this standard SDK variable.
        os.environ["FIREBASE_AUTH_EMULATOR_HOST"] = settings.firebase_auth_emulator_host
    import firebase_admin.auth

    return firebase_admin.auth.verify_id_token(
        token,
        app=_firebase_app(settings.firebase_project_id),
        check_revoked=settings.environment == "production",
    )


def _unauthorized(message: str = "A valid Firebase ID token is required.") -> NexusError:
    return NexusError(ErrorCode.HTTP_ERROR, message, status_code=401)


def _unavailable(message: str = "Firebase identity verification is unavailable.") -> NexusError:
    return NexusError(ErrorCode.DEPENDENCY_UNAVAILABLE, message, status_code=503)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> UserIdentity:
    if credentials is None:
        raise _unauthorized()

    if credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise _unauthorized()
    if not settings.firebase_project_id:
        raise _unavailable("FIREBASE_PROJECT_ID is not configured.")
    verifier: TokenVerifier = getattr(
        request.app.state, "firebase_token_verifier", verify_firebase_id_token
    )
    try:
        claims = verifier(credentials.credentials, settings)
    except Exception as exc:
        # Keep SDK messages out of the response; they can contain token-derived values.
        invalid_token_errors = {
            "ExpiredIdTokenError",
            "InvalidIdTokenError",
            "RevokedIdTokenError",
            "UserDisabledError",
        }
        if type(exc).__name__ in invalid_token_errors or isinstance(exc, ValueError):
            raise _unauthorized("Firebase ID token is invalid or expired.") from None
        logger.error(
            "Firebase ID token verification dependency failed (exception=%s, project_id=%s)",
            type(exc).__name__,
            settings.firebase_project_id,
        )
        raise _unavailable() from None

    uid = claims.get("uid") or claims.get("sub")
    if not isinstance(uid, str) or not uid.strip():
        raise _unauthorized("Firebase ID token has no user identity.")
    role_claim = claims.get("nexus_role")
    try:
        role = UserRole(role_claim)
    except (TypeError, ValueError):
        if settings.environment == "production":
            raise NexusError(
                ErrorCode.HTTP_ERROR,
                "The account has no recognized NEXUS role.",
                status_code=403,
            ) from None
        role = UserRole.OPERATOR
    email = claims.get("email")
    return UserIdentity(
        uid=uid,
        role=role,
        email=email if isinstance(email, str) else None,
    )


def require_roles(
    *roles: UserRole, forbidden_code: ErrorCode = ErrorCode.HTTP_ERROR
) -> Callable[..., Any]:
    allowed = frozenset(roles)

    async def dependency(user: UserIdentity = Depends(get_current_user)) -> UserIdentity:
        if user.role not in allowed:
            raise NexusError(
                forbidden_code,
                "You do not have permission to perform this action.",
                status_code=403,
            )
        return user

    return dependency


require_viewer = require_roles(UserRole.VIEWER, UserRole.OPERATOR, UserRole.ADMIN)
require_operator = require_roles(UserRole.OPERATOR, UserRole.ADMIN)
require_admin = require_roles(UserRole.ADMIN, forbidden_code=ErrorCode.APPROVAL_FORBIDDEN)


__all__ = [
    "UserIdentity",
    "UserRole",
    "get_current_user",
    "require_admin",
    "require_operator",
    "require_roles",
    "require_viewer",
    "verify_firebase_id_token",
]
