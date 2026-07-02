"""SSO login via OIDC: redirect to the IdP, then exchange the callback and mint
our own JWT (reusing the keyring). Registered only when SSO is configured.

`state` (in a short-lived cookie) covers CSRF; `nonce` and the PKCE
`code_verifier` (same cookie treatment) bind the id_token and the authorization
code to this browser's login attempt. The user is JIT-provisioned; an IdP admin
group maps to platform admin.
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
_NONCE_COOKIE = "sso_nonce"
_VERIFIER_COOKIE = "sso_verifier"
_STATE_TTL_SECONDS = 600  # the user must complete the round-trip within 10 minutes


def _redirect_uri(request: Request, configured: str | None) -> str:
    # A configured public callback URL wins (correct behind a reverse proxy, where
    # the request's own host/scheme is the internal one); otherwise derive it.
    if configured:
        return configured
    return str(request.base_url).rstrip("/") + "/sso/callback"


@get("/sso/login", middleware=[build_auth_rate_limit().middleware])
async def sso_login(
    request: Request,
    identity_provider: NamedDependency[IdentityProvider],
    sso_redirect_uri: NamedDependency[str | None],
    sso_cookie_secure: NamedDependency[bool],
) -> Redirect:
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(48)  # PKCE: 43-128 chars after encoding
    url = await identity_provider.authorization_url(
        state,
        _redirect_uri(request, sso_redirect_uri),
        nonce=nonce,
        code_verifier=code_verifier,
    )
    # `Secure` when configured (SESSION_COOKIE_SECURE) or when the request
    # itself is HTTPS — behind a TLS-terminating proxy the app sees HTTP, so
    # the config flag is the reliable signal. `Lax` rides the top-level callback.
    secure = sso_cookie_secure or request.url.scheme == "https"

    def _flow_cookie(key: str, value: str) -> Cookie:
        return Cookie(
            key=key,
            value=value,
            max_age=_STATE_TTL_SECONDS,
            httponly=True,
            secure=secure,
            samesite="lax",
        )

    return Redirect(
        url,
        cookies=[
            _flow_cookie(_STATE_COOKIE, state),
            _flow_cookie(_NONCE_COOKIE, nonce),
            _flow_cookie(_VERIFIER_COOKIE, code_verifier),
        ],
    )


@get("/sso/callback", middleware=[build_auth_rate_limit().middleware])
async def sso_callback(
    request: Request,
    identity_provider: NamedDependency[IdentityProvider],
    user_service: NamedDependency[UserService],
    keyring: NamedDependency[Keyring],
    sso_admin_groups: NamedDependency[tuple[str, ...]],
    sso_redirect_uri: NamedDependency[str | None],
    code: FromQuery[str | None] = None,
    # `state` is a reserved kwarg in Litestar (the app State), so alias the query.
    flow_state: Annotated[str | None, QueryParameter(name="state")] = None,
    # The IdP redirects here with `?error=...` (no code) when the user declines.
    error: FromQuery[str | None] = None,
) -> TokenResponse:
    if not flow_state or request.cookies.get(_STATE_COOKIE) != flow_state:
        raise NotAuthorizedException("Invalid SSO state")
    if error or not code:
        raise NotAuthorizedException("SSO login was not completed")
    nonce = request.cookies.get(_NONCE_COOKIE)
    code_verifier = request.cookies.get(_VERIFIER_COOKIE)
    if not nonce or not code_verifier:
        raise NotAuthorizedException("SSO login flow expired; retry from /sso/login")
    identity = await identity_provider.exchange(
        code,
        _redirect_uri(request, sso_redirect_uri),
        nonce=nonce,
        code_verifier=code_verifier,
    )

    is_admin = bool(set(identity.groups) & set(sso_admin_groups))
    # Email presence/verification and subject-binding rules live in the service.
    user = await user_service.upsert_sso_user(identity, is_admin)
    secret = await keyring.active_jwt_secret()
    access_token, expires_in = issue_access_token(str(user.id), secret, user.token_version)
    return TokenResponse(access_token=access_token, token_type="bearer", expires_in=expires_in)


def create_sso_router() -> Router:
    return Router(path="/", route_handlers=[sso_login, sso_callback], tags=["sso"])
