"""SSO (OIDC) login flow, exercised with a fake identity provider."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import Response
from litestar.status_codes import HTTP_200_OK, HTTP_401_UNAUTHORIZED
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings, TeamGrant
from litestar_gateway.domain.entities import ExternalIdentity, TeamRole
from litestar_gateway.infrastructure.web.session.sso import _resolve_team_grants

ADMIN_GROUP = "gw-admins"


def _identity(
    subject: str,
    email: str,
    *,
    verified: bool = True,
    groups: tuple[str, ...] = (),
) -> ExternalIdentity:
    return ExternalIdentity(subject=subject, email=email, email_verified=verified, groups=groups)


class FakeIdP:
    """Stands in for the OIDC provider: returns a fixed identity on exchange."""

    def __init__(self, identity: ExternalIdentity) -> None:
        self._identity = identity

    async def authorization_url(
        self, state: str, redirect_uri: str, *, nonce: str, code_verifier: str
    ) -> str:
        return f"https://idp.example/authorize?state={state}&nonce={nonce}"

    async def exchange(
        self, code: str, redirect_uri: str, *, nonce: str, code_verifier: str
    ) -> ExternalIdentity:
        return self._identity


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'sso.db'}",
        admin_email="admin@example.com",
        master_key="master-secret",
        jwt_secret="test-secret-key-0123456789-abcdefghij",
        salt_key="test-salt-key",
        oidc_admin_groups=(ADMIN_GROUP,),
    )


def _client(tmp_path: Path, identity: ExternalIdentity) -> AsyncTestClient:
    app = create_app(_settings(tmp_path), identity_provider=FakeIdP(identity))
    return AsyncTestClient(app=app)


async def _login_state(client: AsyncTestClient) -> str:
    resp = await client.get("/sso/login", follow_redirects=False)
    assert resp.status_code in (301, 302, 303, 307)
    state = client.cookies.get("sso_state")
    assert state
    return state


async def _callback(client: AsyncTestClient) -> Response:
    state = await _login_state(client)
    return await client.get(f"/sso/callback?code=abc&state={state}")


_MASTER_KEY = "master-secret"  # pragma: allowlist secret


async def _admin_token(client: AsyncTestClient) -> str:
    """Log in as the bootstrap platform admin (password == master key on first boot)."""
    resp = await client.post("/login", json={"email": "admin@example.com", "password": _MASTER_KEY})
    return resp.json()["access_token"]


async def _create_team(client: AsyncTestClient) -> str:
    """As the bootstrap admin, create an org + team; return the team's id."""
    headers = {"Authorization": f"Bearer {await _admin_token(client)}"}
    org_id = (await client.post("/organizations", json={"name": "Acme"}, headers=headers)).json()[
        "id"
    ]
    team = await client.post(
        f"/organizations/{org_id}/teams",
        json={"name": "Core", "admin_email": "admin@example.com"},
        headers=headers,
    )
    return team.json()["id"]


async def _team_membership(client: AsyncTestClient, team_id: str, user_id: str) -> dict | None:
    """The user's membership row in `team_id` (as seen by the platform admin), or None."""
    headers = {"Authorization": f"Bearer {await _admin_token(client)}"}
    members = (await client.get(f"/teams/{team_id}/members", headers=headers)).json()
    return next((m for m in members if m["user_id"] == user_id), None)


async def _login_and_get_me(tmp_path: Path, identity: ExternalIdentity) -> dict:
    """Run a full login for `identity` against the (shared) tmp_path DB, return /me."""
    async with _client(tmp_path, identity) as client:
        token = (await _callback(client)).json()["access_token"]
        me = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == HTTP_200_OK
        return me.json()


@pytest.fixture
async def admin_identity_client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    identity = _identity("s1", "alice@corp.com", groups=(ADMIN_GROUP,))
    async with _client(tmp_path, identity) as client:
        yield client


async def test_sso_callback_jit_creates_admin_and_issues_jwt(
    admin_identity_client: AsyncTestClient,
) -> None:
    client = admin_identity_client
    callback = await _callback(client)
    assert callback.status_code == HTTP_200_OK
    token = callback.json()["access_token"]

    # The JIT-provisioned user can use the token; the IdP admin group → platform admin.
    me = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == HTTP_200_OK
    assert me.json()["email"] == "alice@corp.com"
    assert me.json()["is_admin"] is True


async def test_sso_callback_clears_flow_cookies(
    admin_identity_client: AsyncTestClient,
) -> None:
    # L28: the one-time flow cookies must not linger after the callback consumes them.
    client = admin_identity_client
    state = await _login_state(client)
    assert client.cookies.get("sso_state")  # set by /sso/login
    resp = await client.get(f"/sso/callback?code=abc&state={state}")
    assert resp.status_code == HTTP_200_OK
    # Cleared (Set-Cookie max-age=0) -> httpx drops them from the jar.
    assert client.cookies.get("sso_state") is None
    assert client.cookies.get("sso_nonce") is None
    assert client.cookies.get("sso_verifier") is None


async def test_sso_callback_rejects_state_mismatch(
    admin_identity_client: AsyncTestClient,
) -> None:
    client = admin_identity_client
    await _login_state(client)  # sets the cookie
    resp = await client.get("/sso/callback?code=abc&state=not-the-cookie-state")
    assert resp.status_code == HTTP_401_UNAUTHORIZED


async def test_sso_callback_rejects_idp_error_without_code(
    admin_identity_client: AsyncTestClient,
) -> None:
    # User declined consent: the IdP redirects back with an error and no code.
    # A matching state must still yield a clean 401, not a 400 validation error.
    client = admin_identity_client
    state = await _login_state(client)
    resp = await client.get(f"/sso/callback?error=access_denied&state={state}")
    assert resp.status_code == HTTP_401_UNAUTHORIZED


async def test_sso_non_admin_group_creates_regular_user(tmp_path: Path) -> None:
    me = await _login_and_get_me(tmp_path, _identity("s2", "bob@corp.com", groups=("other",)))
    assert me["email"] == "bob@corp.com"
    assert me["is_admin"] is False


async def test_sso_rejects_unverified_email(tmp_path: Path) -> None:
    # Even claiming the bootstrap admin's email, an unverified claim is refused —
    # this is the account-takeover guard: SSO never trusts an unverified address.
    identity = _identity("attacker", "admin@example.com", verified=False, groups=(ADMIN_GROUP,))
    async with _client(tmp_path, identity) as client:
        assert (await _callback(client)).status_code == HTTP_401_UNAUTHORIZED


async def test_sso_resyncs_admin_role_on_relogin(tmp_path: Path) -> None:
    subject = "s-resync"
    # First login: not in the admin group ⇒ regular user.
    first = await _login_and_get_me(tmp_path, _identity(subject, "carol@corp.com"))
    assert first["is_admin"] is False
    # Second login (same subject) now in the admin group ⇒ promoted.
    second = await _login_and_get_me(
        tmp_path, _identity(subject, "carol@corp.com", groups=(ADMIN_GROUP,))
    )
    assert second["is_admin"] is True
    # And demoted again when dropped from the group (IdP is the source of truth).
    third = await _login_and_get_me(tmp_path, _identity(subject, "carol@corp.com"))
    assert third["is_admin"] is False


async def test_sso_binds_to_subject_across_email_change(tmp_path: Path) -> None:
    subject = "s-stable"
    await _login_and_get_me(tmp_path, _identity(subject, "dave@corp.com"))
    # Same subject, new email ⇒ resolves to the same account, bound by `sub`.
    me = await _login_and_get_me(tmp_path, _identity(subject, "dave.new@corp.com"))
    assert me["email"] == "dave@corp.com"  # email not overwritten; account is the same


async def test_sso_rejects_email_bound_to_other_subject(tmp_path: Path) -> None:
    await _login_and_get_me(tmp_path, _identity("sub-A", "eve@corp.com"))
    # A different subject asserting the same email must not adopt the account.
    async with _client(tmp_path, _identity("sub-B", "eve@corp.com")) as client:
        assert (await _callback(client)).status_code == HTTP_401_UNAUTHORIZED


async def test_sso_uses_configured_redirect_uri(tmp_path: Path) -> None:
    # Behind a proxy the operator pins the public callback URL; the IdP must see it
    # rather than the request-derived (internal) one.
    public = "https://gateway.example.com/sso/callback"
    captured: dict[str, str] = {}

    class RecordingIdP(FakeIdP):
        async def authorization_url(
            self, state: str, redirect_uri: str, *, nonce: str, code_verifier: str
        ) -> str:
            captured["authorize"] = redirect_uri
            return await super().authorization_url(
                state, redirect_uri, nonce=nonce, code_verifier=code_verifier
            )

        async def exchange(
            self, code: str, redirect_uri: str, *, nonce: str, code_verifier: str
        ) -> ExternalIdentity:
            captured["exchange"] = redirect_uri
            return await super().exchange(
                code, redirect_uri, nonce=nonce, code_verifier=code_verifier
            )

    settings = dataclasses.replace(_settings(tmp_path), oidc_redirect_uri=public)
    idp = RecordingIdP(_identity("s-cfg", "frank@corp.com"))
    async with AsyncTestClient(app=create_app(settings, identity_provider=idp)) as client:
        await _callback(client)
    assert captured["authorize"] == public
    assert captured["exchange"] == public


async def test_sso_state_cookie_secure_when_configured(tmp_path: Path) -> None:
    # With SESSION_COOKIE_SECURE the state cookie is marked Secure even though the
    # test client speaks HTTP (the case behind a TLS-terminating proxy).
    settings = dataclasses.replace(_settings(tmp_path), session_cookie_secure=True)
    idp = FakeIdP(_identity("s-sec", "sec@corp.com"))
    async with AsyncTestClient(app=create_app(settings, identity_provider=idp)) as client:
        resp = await client.get("/sso/login", follow_redirects=False)
    set_cookie = resp.headers.get("set-cookie", "")
    assert "sso_state=" in set_cookie
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie


async def test_sso_state_cookie_not_secure_over_http_by_default(tmp_path: Path) -> None:
    # Default (local/dev): no Secure flag, so the cookie works over plain HTTP.
    idp = FakeIdP(_identity("s-plain", "plain@corp.com"))
    async with AsyncTestClient(
        app=create_app(_settings(tmp_path), identity_provider=idp)
    ) as client:
        resp = await client.get("/sso/login", follow_redirects=False)
    assert "Secure" not in resp.headers.get("set-cookie", "")


def test_resolve_team_grants_admin_wins_and_governed_is_codomain() -> None:
    t1, t2 = uuid4(), uuid4()
    mapping: dict[str, tuple[TeamGrant, ...]] = {
        "eng": (TeamGrant(t1, TeamRole.MEMBER),),
        "eng-leads": (TeamGrant(t1, TeamRole.ADMIN), TeamGrant(t2, TeamRole.MEMBER)),
    }
    desired, governed = _resolve_team_grants(("eng", "eng-leads"), mapping)
    # ADMIN wins for t1 even though "eng" also grants it as MEMBER.
    assert desired == {t1: TeamRole.ADMIN, t2: TeamRole.MEMBER}
    assert governed == {t1, t2}


def test_resolve_team_grants_governed_covers_unmatched_groups() -> None:
    # A user in none of the mapped groups still yields the full governed set, so
    # reconciliation can strip stale memberships from governed teams.
    t1 = uuid4()
    desired, governed = _resolve_team_grants(
        ("unmapped",), {"eng": (TeamGrant(t1, TeamRole.MEMBER),)}
    )
    assert desired == {}
    assert governed == {t1}


async def _me_id(client: AsyncTestClient, token: str) -> str:
    return (await client.get("/me", headers={"Authorization": f"Bearer {token}"})).json()["id"]


async def test_sso_group_mapping_provisions_team_membership(tmp_path: Path) -> None:
    # End-to-end: a bootstrap admin creates a team; SSO_TEAM_MAPPING maps an IdP
    # group to it; an SSO user in that group is auto-provisioned as a team admin.
    base = _settings(tmp_path)
    async with AsyncTestClient(app=create_app(base)) as admin_client:
        team_id = await _create_team(admin_client)

    mapping: dict[str, tuple[TeamGrant, ...]] = {
        "engineering": (TeamGrant(UUID(team_id), TeamRole.ADMIN),)
    }
    settings = dataclasses.replace(base, oidc_team_mapping=mapping)
    identity = _identity("s-map", "grace@corp.com", groups=("engineering",))
    async with AsyncTestClient(
        app=create_app(settings, identity_provider=FakeIdP(identity))
    ) as client:
        grace_id = await _me_id(client, (await _callback(client)).json()["access_token"])
        membership = await _team_membership(client, team_id, grace_id)

    assert membership is not None
    assert membership["role"] == "admin"


async def test_sso_relogin_without_group_removes_team_membership(tmp_path: Path) -> None:
    # Deprovisioning: once the user drops out of the mapped group, the next login
    # reconciles the governed team and removes the membership (IdP is authoritative).
    base = _settings(tmp_path)
    async with AsyncTestClient(app=create_app(base)) as admin_client:
        team_id = await _create_team(admin_client)

    # Map the group to MEMBER so the SSO user is never the team's (sole) admin —
    # otherwise the last-admin guard would (correctly) keep the membership.
    mapping: dict[str, tuple[TeamGrant, ...]] = {
        "engineering": (TeamGrant(UUID(team_id), TeamRole.MEMBER),)
    }
    settings = dataclasses.replace(base, oidc_team_mapping=mapping)

    async def _membership_with(groups: tuple[str, ...]) -> dict | None:
        identity = _identity("s-drop", "heidi@corp.com", groups=groups)
        async with AsyncTestClient(
            app=create_app(settings, identity_provider=FakeIdP(identity))
        ) as client:
            user_id = await _me_id(client, (await _callback(client)).json()["access_token"])
            return await _team_membership(client, team_id, user_id)

    assert await _membership_with(("engineering",)) is not None  # provisioned in
    assert await _membership_with(()) is None  # then removed when the group is gone


async def test_sso_routes_absent_when_not_configured(tmp_path: Path) -> None:
    # No identity provider and no OIDC config ⇒ the SSO routes are not registered.
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'nosso.db'}",
        admin_email="admin@example.com",
        master_key="master-secret",
        jwt_secret="test-secret-key-0123456789-abcdefghij",
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as client:
        assert (await client.get("/sso/login", follow_redirects=False)).status_code == 404
