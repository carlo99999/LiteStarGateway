"""Shared fixtures for SSO (OIDC) tests: a fake identity provider + helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import Response
from litestar.status_codes import HTTP_200_OK
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings
from litestar_gateway.domain.entities import ExternalIdentity

ADMIN_GROUP = "gw-admins"
_MASTER_KEY = "master-secret"  # pragma: allowlist secret


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
        master_key=_MASTER_KEY,
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
