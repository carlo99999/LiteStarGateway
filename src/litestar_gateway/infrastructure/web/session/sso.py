"""SSO login via OIDC: redirect to the IdP, then exchange the callback and mint
our own JWT (reusing the keyring). Registered only when SSO is configured.

`state` (in a short-lived cookie) covers CSRF; `nonce` and the PKCE
`code_verifier` (same cookie treatment) bind the id_token and the authorization
code to this browser's login attempt. The user is JIT-provisioned; an IdP admin
group (or the DEFAULT_ROLE fallback for a new account) grants platform admin, and
the admin flag is upgrade-only — re-login never revokes it.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from litestar import Request, get
from litestar.datastructures import Cookie
from litestar.di import NamedDependency
from litestar.exceptions import NotAuthorizedException
from litestar.params import FromQuery, QueryParameter
from litestar.response import Redirect
from litestar.router import Router

from litestar_gateway.application.team_service import TeamService
from litestar_gateway.application.user_service import UserService
from litestar_gateway.config import TeamGrant
from litestar_gateway.domain.entities import AuditEvent, TeamRole, User
from litestar_gateway.domain.ports import AuditLog, IdentityProvider
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.web.rate_limit import build_auth_rate_limit
from litestar_gateway.infrastructure.web.session.jwt import issue_access_token
from litestar_gateway.infrastructure.web.session.schemas import TokenResponse

_STATE_COOKIE = "sso_state"
_NONCE_COOKIE = "sso_nonce"
_VERIFIER_COOKIE = "sso_verifier"
_STATE_TTL_SECONDS = 600  # the user must complete the round-trip within 10 minutes

# Once the callback runs the one-time flow cookies are spent; expire them so a
# leftover copy on a shared/kiosk browser can't be replayed within the TTL (L28).
_EXPIRE_FLOW_COOKIES = [
    Cookie(key=key, value="", max_age=0, httponly=True, samesite="lax")
    for key in (_STATE_COOKIE, _NONCE_COOKIE, _VERIFIER_COOKIE)
]


def _resolve_team_grants(
    groups: Iterable[str], mapping: dict[str, tuple[TeamGrant, ...]]
) -> tuple[dict[UUID, TeamRole], set[UUID]]:
    """From the user's IdP groups and SSO_TEAM_MAPPING, compute the desired
    (team -> role) grants and the set of SSO-governed teams (the mapping's
    codomain). ADMIN wins when the user's groups grant one team at both roles."""
    governed = {grant.team_id for grants in mapping.values() for grant in grants}
    desired: dict[UUID, TeamRole] = {}
    for group in groups:
        for grant in mapping.get(group, ()):
            if desired.get(grant.team_id) is None or grant.role is TeamRole.ADMIN:
                desired[grant.team_id] = grant.role
    return desired, governed


async def _record_sso_audit(
    audit_log: AuditLog,
    request: Request,
    user: User,
    action: str,
    *,
    target_type: str,
    target_id: UUID,
    detail: str | None = None,
) -> None:
    """SSO changes are IdP-driven, not an authenticated API call: the actor is the
    logging-in user's verified SSO identity, marked `actor_type="sso"` (mirrors
    SCIM's `scim_token` convention) so escalations granted by IdP group membership
    are attributable to the SSO path in the trail."""
    await audit_log.record(
        AuditEvent(
            id=uuid4(),
            action=action,
            actor_id=user.id,
            actor_type="sso",
            actor_email=f"sso:{user.email}",
            target_type=target_type,
            target_id=str(target_id),
            ip=request.client.host if request.client else None,
            detail=detail,
            created_at=datetime.now(UTC),
        )
    )


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


@get(
    "/sso/callback",
    middleware=[build_auth_rate_limit().middleware],
    response_cookies=_EXPIRE_FLOW_COOKIES,
)
async def sso_callback(
    request: Request,
    identity_provider: NamedDependency[IdentityProvider],
    user_service: NamedDependency[UserService],
    team_service: NamedDependency[TeamService],
    keyring: NamedDependency[Keyring],
    sso_admin_groups: NamedDependency[tuple[str, ...]],
    sso_default_admin: NamedDependency[bool],
    sso_team_mapping: NamedDependency[dict[str, tuple[TeamGrant, ...]]],
    sso_redirect_uri: NamedDependency[str | None],
    audit_log: NamedDependency[AuditLog],
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

    group_admin = bool(set(identity.groups) & set(sso_admin_groups))
    # Email/verification, subject binding, and the upgrade-only admin sync live in
    # the service; DEFAULT_ROLE seeds only a brand-new account's platform role.
    result = await user_service.upsert_sso_user(
        identity, group_admin=group_admin, default_admin=sso_default_admin
    )
    user = result.user
    # Audit what this login actually changed (R6-H20): JIT creation and admin
    # escalation are the moves an attacker with IdP group control would make.
    if result.created:
        await _record_sso_audit(
            audit_log, request, user, "sso.user.create", target_type="user", target_id=user.id
        )
    if result.admin_granted:
        await _record_sso_audit(
            audit_log,
            request,
            user,
            "sso.user.grant_admin",
            target_type="user",
            target_id=user.id,
            detail="via IdP admin group" if group_admin else "via DEFAULT_ROLE",
        )
    # Group → team/role mapping: reconcile the user's memberships in SSO-governed
    # teams to their current IdP groups (add/update/remove); a no-op when unset.
    desired, governed = _resolve_team_grants(identity.groups, sso_team_mapping)
    if governed:
        changes = await team_service.reconcile_sso_memberships(user.id, desired, governed)
        for change in changes:
            if change.change == "add":
                action, detail = "sso.team.member.add", f"{user.email} as {change.role}"
            elif change.change == "update":
                action, detail = "sso.team.member.set_role", f"user {user.id} -> {change.role}"
            else:
                action, detail = "sso.team.member.remove", f"user {user.id}"
            await _record_sso_audit(
                audit_log,
                request,
                user,
                action,
                target_type="team",
                target_id=change.team_id,
                detail=detail,
            )
    secret = await keyring.active_jwt_secret()
    access_token, expires_in = issue_access_token(str(user.id), secret, user.token_version)
    return TokenResponse(access_token=access_token, token_type="bearer", expires_in=expires_in)


def create_sso_router() -> Router:
    return Router(path="/", route_handlers=[sso_login, sso_callback], tags=["sso"])
