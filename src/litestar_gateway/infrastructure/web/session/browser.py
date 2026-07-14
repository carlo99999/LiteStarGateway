"""Browser-only admin session endpoints using an HttpOnly cookie and CSRF token."""

from __future__ import annotations

import secrets

from litestar import Request, Response, get, post
from litestar.di import NamedDependency, Provide
from litestar.exceptions import NotAuthorizedException
from litestar.status_codes import HTTP_200_OK, HTTP_204_NO_CONTENT

from litestar_gateway.application.user_service import UserService
from litestar_gateway.domain.exceptions import InvalidCredentials
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.web.rate_limit import build_auth_rate_limit
from litestar_gateway.infrastructure.web.session.cookies import (
    browser_session_cookie,
    browser_session_is_secure,
    expired_browser_session_cookie,
)
from litestar_gateway.infrastructure.web.session.dependencies import (
    AuthenticatedUser,
    provide_browser_session,
    require_same_origin,
)
from litestar_gateway.infrastructure.web.session.jwt import (
    ACCESS_TOKEN_TTL,
    issue_browser_session,
)
from litestar_gateway.infrastructure.web.session.schemas import (
    BrowserSessionResponse,
    LoginRequest,
)
from litestar_gateway.infrastructure.web.users.schemas import UserResponse


def _response(authenticated: AuthenticatedUser) -> BrowserSessionResponse:
    assert authenticated.csrf_token is not None
    return BrowserSessionResponse(
        user=UserResponse.from_entity(authenticated.user),
        csrf_token=authenticated.csrf_token,
        expires_in=int(ACCESS_TOKEN_TTL.total_seconds()),
    )


@post(
    "/session/login",
    status_code=HTTP_200_OK,
    middleware=[build_auth_rate_limit().middleware],
)
async def browser_login(
    request: Request,
    data: LoginRequest,
    user_service: NamedDependency[UserService],
    keyring: NamedDependency[Keyring],
    browser_session_cookie_secure: NamedDependency[bool],
) -> Response[BrowserSessionResponse]:
    require_same_origin(request)
    try:
        user = await user_service.authenticate(data.email, data.password)
    except InvalidCredentials as exc:
        raise NotAuthorizedException("Invalid email or password") from exc

    csrf_token = secrets.token_urlsafe(32)
    secret = await keyring.active_jwt_secret()
    encoded, expires_in = issue_browser_session(
        str(user.id), secret, user.token_version, csrf_token
    )
    secure = browser_session_is_secure(request, configured=browser_session_cookie_secure)
    content = BrowserSessionResponse(
        user=UserResponse.from_entity(user),
        csrf_token=csrf_token,
        expires_in=expires_in,
    )
    return Response(
        content,
        cookies=[browser_session_cookie(encoded, secure=secure, max_age=expires_in)],
        headers={"Cache-Control": "no-store"},
    )


@get(
    "/session",
    dependencies={"browser_session": Provide(provide_browser_session)},
)
async def get_browser_session(
    browser_session: NamedDependency[AuthenticatedUser],
) -> Response[BrowserSessionResponse]:
    return Response(_response(browser_session), headers={"Cache-Control": "no-store"})


@post(
    "/session/logout",
    status_code=HTTP_204_NO_CONTENT,
    dependencies={"browser_session": Provide(provide_browser_session)},
)
async def browser_logout(
    request: Request,
    browser_session: NamedDependency[AuthenticatedUser],
    user_service: NamedDependency[UserService],
    browser_session_cookie_secure: NamedDependency[bool],
) -> Response[None]:
    await user_service.logout(browser_session.user)
    secure = browser_session_is_secure(request, configured=browser_session_cookie_secure)
    return Response(
        None,
        status_code=HTTP_204_NO_CONTENT,
        cookies=[expired_browser_session_cookie(secure=secure)],
        headers={"Cache-Control": "no-store"},
    )
