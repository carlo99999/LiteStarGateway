"""SSO (OIDC) login flow, exercised with a fake identity provider."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_401_UNAUTHORIZED
from litestar.testing import AsyncTestClient

from litestar_test.app import create_app
from litestar_test.config import Settings
from litestar_test.domain.entities import ExternalIdentity

ADMIN_GROUP = "gw-admins"


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


@pytest.fixture
async def admin_identity_client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    identity = ExternalIdentity(subject="s1", email="alice@corp.com", groups=(ADMIN_GROUP,))
    async with _client(tmp_path, identity) as client:
        yield client


async def test_sso_callback_jit_creates_admin_and_issues_jwt(
    admin_identity_client: AsyncTestClient,
) -> None:
    client = admin_identity_client
    state = await _login_state(client)

    callback = await client.get(f"/sso/callback?code=abc&state={state}")
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


async def test_sso_non_admin_group_creates_regular_user(tmp_path: Path) -> None:
    identity = ExternalIdentity(subject="s2", email="bob@corp.com", groups=("some-other-group",))
    async with _client(tmp_path, identity) as client:
        state = await _login_state(client)
        token = (await client.get(f"/sso/callback?code=abc&state={state}")).json()["access_token"]
        me = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
        assert me.json()["email"] == "bob@corp.com"
        assert me.json()["is_admin"] is False


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
