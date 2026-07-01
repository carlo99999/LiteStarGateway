"""Dependencies for login sessions: resolve the current user from a JWT."""

from __future__ import annotations

from uuid import UUID

from litestar import Request
from litestar.di import NamedDependency
from litestar.exceptions import NotAuthorizedException, PermissionDeniedException

from litestar_test.application.user_service import UserService
from litestar_test.domain.entities import User
from litestar_test.infrastructure.keyring import Keyring
from litestar_test.infrastructure.web.session.jwt import decode_token


async def provide_current_user(
    request: Request,
    keyring: NamedDependency[Keyring],
    user_service: NamedDependency[UserService],
) -> User:
    """Authenticate via `Authorization: Bearer <jwt>` and load the user."""
    auth = request.headers.get("Authorization")
    if not auth:
        raise NotAuthorizedException("Missing bearer token")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise NotAuthorizedException("Invalid Authorization header")

    secrets = await keyring.jwt_verification_secrets()
    subject, token_version = decode_token(token, secrets)  # raises on invalid/expired
    try:
        user_id = UUID(subject)
    except ValueError as exc:
        raise NotAuthorizedException("Invalid token subject") from exc

    user = await user_service.get_by_id(user_id)
    if user is None:
        raise NotAuthorizedException("User no longer exists")
    # Reject tokens issued before a logout (token_version bump).
    if token_version != user.token_version:
        raise NotAuthorizedException("Token has been revoked")
    return user


async def provide_current_admin(
    request: Request,
    keyring: NamedDependency[Keyring],
    user_service: NamedDependency[UserService],
) -> User:
    """Like `provide_current_user`, but rejects non-admin users with 403."""
    user = await provide_current_user(request, keyring, user_service)
    if not user.is_admin:
        raise PermissionDeniedException("Admin privileges required")
    return user
