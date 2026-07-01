"""SSO login via OIDC: redirect to the IdP, then exchange the callback and mint
our own JWT (reusing the keyring). Registered only when SSO is configured.

`state` (in a short-lived cookie) covers CSRF. The user is JIT-provisioned; an
IdP admin group maps to platform admin.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from litestar import Request, get
from litestar.datastructures import Cookie
from litestar.di import NamedDependency
from litestar.exceptions import NotAuthorizedException
from litestar.params import FromQuery, QueryParameter
from litestar.response import Redirect
from litestar.router import Router

from litestar_test.application.user_service import UserService
from litestar_test.domain.ports import IdentityProvider
from litestar_test.infrastructure.keyring import Keyring
from litestar_test.infrastructure.web.rate_limit import build_auth_rate_limit
from litestar_test.infrastructure.web.session.jwt import issue_access_token
from litestar_test.infrastructure.web.session.schemas import TokenResponse

_STATE_COOKIE = "sso_state"


def _redirect_uri(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/sso/callback"


@get("/sso/login", middleware=[build_auth_rate_limit().middleware])
async def sso_login(
    request: Request, identity_provider: NamedDependency[IdentityProvider]
) -> Redirect:
    state = secrets.token_urlsafe(32)
    url = await identity_provider.authorization_url(state, _redirect_uri(request))
    return Redirect(
        url,
        cookies=[
            # secure=True behind TLS in production; Lax lets it ride the callback.
            Cookie(key=_STATE_COOKIE, value=state, max_age=600, httponly=True, samesite="lax")
        ],
    )


@get("/sso/callback", middleware=[build_auth_rate_limit().middleware])
async def sso_callback(
    request: Request,
    code: FromQuery[str],
    # `state` is a reserved kwarg in Litestar (the app State), so alias the query.
    flow_state: Annotated[str, QueryParameter(name="state")],
    identity_provider: NamedDependency[IdentityProvider],
    user_service: NamedDependency[UserService],
    keyring: NamedDependency[Keyring],
    sso_admin_groups: NamedDependency[tuple[str, ...]],
) -> TokenResponse:
    if not flow_state or request.cookies.get(_STATE_COOKIE) != flow_state:
        raise NotAuthorizedException("Invalid SSO state")
    identity = await identity_provider.exchange(code, _redirect_uri(request))

    is_admin = bool(set(identity.groups) & set(sso_admin_groups))
    # Email presence/verification and subject-binding rules live in the service.
    user = await user_service.upsert_sso_user(identity, is_admin)
    secret = await keyring.active_jwt_secret()
    access_token, expires_in = issue_access_token(str(user.id), secret, user.token_version)
    return TokenResponse(access_token=access_token, token_type="bearer", expires_in=expires_in)


def create_sso_router() -> Router:
    return Router(path="/", route_handlers=[sso_login, sso_callback], tags=["sso"])
