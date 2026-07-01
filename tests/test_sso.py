"""SSO (OIDC) login flow, exercised with a fake identity provider."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import Response
from litestar.status_codes import HTTP_200_OK, HTTP_401_UNAUTHORIZED
from litestar.testing import AsyncTestClient

from litestar_test.app import create_app
from litestar_test.config import Settings
from litestar_test.domain.entities import ExternalIdentity

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

    async def authorization_url(self, state: str, redirect_uri: str) -> str:
        return f"https://idp.example/authorize?state={state}"

    async def exchange(self, code: str, redirect_uri: str) -> ExternalIdentity:
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
        async def authorization_url(self, state: str, redirect_uri: str) -> str:
            captured["authorize"] = redirect_uri
            return await super().authorization_url(state, redirect_uri)

        async def exchange(self, code: str, redirect_uri: str) -> ExternalIdentity:
            captured["exchange"] = redirect_uri
            return await super().exchange(code, redirect_uri)

    settings = dataclasses.replace(_settings(tmp_path), oidc_redirect_uri=public)
    idp = RecordingIdP(_identity("s-cfg", "frank@corp.com"))
    async with AsyncTestClient(app=create_app(settings, identity_provider=idp)) as client:
        await _callback(client)
    assert captured["authorize"] == public
    assert captured["exchange"] == public


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
