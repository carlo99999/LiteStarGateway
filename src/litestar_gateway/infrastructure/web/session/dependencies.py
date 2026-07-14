"""Dependencies for login sessions: resolve the current user from a JWT."""

from __future__ import annotations

from dataclasses import dataclass
from secrets import compare_digest
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from litestar import Request
from litestar.di import NamedDependency
from litestar.exceptions import NotAuthorizedException, PermissionDeniedException

from litestar_gateway.application.user_service import UserService
from litestar_gateway.domain.entities import User
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.web.session.cookies import (
    browser_session_cookie_name,
    browser_session_is_secure,
)
from litestar_gateway.infrastructure.web.session.jwt import decode_browser_session, decode_token

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class AuthenticatedUser:
    user: User
    transport: Literal["bearer", "cookie"]
    csrf_token: str | None = None


def require_same_origin(request: Request) -> None:
    """Reject browser state changes without an exact same-host Origin."""
    origin = request.headers.get("Origin")
    host = request.headers.get("Host")
    if not origin or not host:
        raise PermissionDeniedException("Same-origin request required")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or parsed.netloc != host:
        raise PermissionDeniedException("Same-origin request required")
    if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        raise PermissionDeniedException("Same-origin request required")


def _require_cookie_csrf(request: Request, expected: str) -> None:
    require_same_origin(request)
    supplied = request.headers.get("X-CSRF-Token", "")
    if not supplied or not compare_digest(supplied, expected):
        raise PermissionDeniedException("Invalid CSRF token")


async def _load_user(subject: str, token_version: int, user_service: UserService) -> User:
    try:
        user_id = UUID(subject)
    except ValueError as exc:
        raise NotAuthorizedException("Invalid token subject") from exc

    user = await user_service.get_by_id(user_id)
    if user is None:
        raise NotAuthorizedException("User no longer exists")
    if token_version != user.token_version:
        raise NotAuthorizedException("Token has been revoked")
    if not user.is_active:
        raise NotAuthorizedException("Account is disabled")
    return user


async def provide_authenticated_user(
    request: Request,
    keyring: Keyring,
    user_service: UserService,
    browser_session_cookie_secure: bool,
) -> AuthenticatedUser:
    """Resolve an explicit bearer JWT or, if absent, a browser cookie session."""
    auth = request.headers.get("Authorization")
    secrets = await keyring.jwt_verification_secrets()
    if auth is not None:
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise NotAuthorizedException("Invalid Authorization header")
        subject, token_version = decode_token(token, secrets)
        user = await _load_user(subject, token_version, user_service)
        return AuthenticatedUser(user=user, transport="bearer")

    secure = browser_session_is_secure(request, configured=browser_session_cookie_secure)
    cookie_name = browser_session_cookie_name(secure=secure)
    encoded_session = request.cookies.get(cookie_name)
    if not encoded_session:
        raise NotAuthorizedException("Missing authentication credentials")
    subject, token_version, csrf_token = decode_browser_session(encoded_session, secrets)
    user = await _load_user(subject, token_version, user_service)
    if request.method.upper() not in SAFE_METHODS:
        _require_cookie_csrf(request, csrf_token)
    return AuthenticatedUser(user=user, transport="cookie", csrf_token=csrf_token)


async def provide_current_user(
    request: Request,
    keyring: NamedDependency[Keyring],
    user_service: NamedDependency[UserService],
    browser_session_cookie_secure: NamedDependency[bool],
) -> User:
    """Authenticate a human via explicit bearer JWT or browser cookie session."""
    authenticated = await provide_authenticated_user(
        request, keyring, user_service, browser_session_cookie_secure
    )
    return authenticated.user


async def provide_browser_session(
    request: Request,
    keyring: NamedDependency[Keyring],
    user_service: NamedDependency[UserService],
    browser_session_cookie_secure: NamedDependency[bool],
) -> AuthenticatedUser:
    authenticated = await provide_authenticated_user(
        request, keyring, user_service, browser_session_cookie_secure
    )
    if authenticated.transport != "cookie":
        raise NotAuthorizedException("Browser session cookie required")
    return authenticated


async def provide_current_admin(
    request: Request,
    keyring: NamedDependency[Keyring],
    user_service: NamedDependency[UserService],
    browser_session_cookie_secure: NamedDependency[bool],
) -> User:
    """Like `provide_current_user`, but rejects non-admin users with 403."""
    user = await provide_current_user(request, keyring, user_service, browser_session_cookie_secure)
    if not user.is_admin:
        raise PermissionDeniedException("Admin privileges required")
    return user


async def provide_audit_reader(
    request: Request,
    keyring: NamedDependency[Keyring],
    user_service: NamedDependency[UserService],
    browser_session_cookie_secure: NamedDependency[bool],
) -> User:
    """Like `provide_current_user`, but requires the platform-admin or the
    read-only platform-auditor role (403 otherwise)."""
    user = await provide_current_user(request, keyring, user_service, browser_session_cookie_secure)
    if not (user.is_admin or user.is_auditor):
        raise PermissionDeniedException("Admin or auditor privileges required")
    return user
