"""Integration tests for login / JWT sessions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from _invite_helpers import issue_invite, seed_team
from litestar.status_codes import HTTP_200_OK, HTTP_401_UNAUTHORIZED
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

MASTER_KEY = "master-secret"
ADMIN_EMAIL = "admin@example.com"
SEVEN_DAYS = 7 * 24 * 60 * 60


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    # Explicit Settings keep tests isolated from the developer's env / .env file.
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'sess.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


async def _admin_token(client: AsyncTestClient) -> str:
    resp = await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    return resp.json()["access_token"]


async def _signup(client: AsyncTestClient, email: str, password: str) -> None:
    admin = await _admin_token(client)
    team_id = await seed_team(client, admin)
    token = await issue_invite(client, admin, team_id)
    resp = await client.post(
        "/signup",
        json={"invite_token": token, "email": email, "password": password},
    )
    assert resp.status_code == 201


async def test_login_success_returns_jwt(client: AsyncTestClient) -> None:
    await _signup(client, "alice@b.com", "S3cret!!")
    resp = await client.post("/login", json={"email": "alice@b.com", "password": "S3cret!!"})
    assert resp.status_code == HTTP_200_OK
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == SEVEN_DAYS
    assert body["access_token"]


async def test_login_wrong_password(client: AsyncTestClient) -> None:
    await _signup(client, "alice@b.com", "S3cret!!")
    resp = await client.post("/login", json={"email": "alice@b.com", "password": "nope"})
    assert resp.status_code == HTTP_401_UNAUTHORIZED


async def test_login_unknown_email(client: AsyncTestClient) -> None:
    resp = await client.post("/login", json={"email": "ghost@b.com", "password": "x"})
    assert resp.status_code == HTTP_401_UNAUTHORIZED


async def test_me_requires_token(client: AsyncTestClient) -> None:
    assert (await client.get("/me")).status_code == HTTP_401_UNAUTHORIZED


async def test_me_rejects_garbage_token(client: AsyncTestClient) -> None:
    resp = await client.get("/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == HTTP_401_UNAUTHORIZED


async def test_me_teams_lists_only_own_memberships(client: AsyncTestClient) -> None:
    # Signup via invite joins its team; /me/teams shows that membership (and
    # only the caller's own) with the role from the invite.
    await _signup(client, "alice@b.com", "S3cret!!")
    token = (
        await client.post("/login", json={"email": "alice@b.com", "password": "S3cret!!"})
    ).json()["access_token"]
    resp = await client.get("/me/teams", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == HTTP_200_OK
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["role"] == "member"
    assert rows[0]["name"]
    assert rows[0]["team_id"]


async def test_me_returns_current_user(client: AsyncTestClient) -> None:
    await _signup(client, "alice@b.com", "S3cret!!")
    token = (
        await client.post("/login", json={"email": "alice@b.com", "password": "S3cret!!"})
    ).json()["access_token"]
    resp = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == HTTP_200_OK
    body = resp.json()
    assert body["email"] == "alice@b.com"
    assert body["is_admin"] is False


async def test_admin_disable_locks_user_out(client: AsyncTestClient) -> None:
    await _signup(client, "bob@b.com", "S3cret!!")
    token = (
        await client.post("/login", json={"email": "bob@b.com", "password": "S3cret!!"})
    ).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    user_id = (await client.get("/me", headers=auth)).json()["id"]
    admin = {"Authorization": f"Bearer {await _admin_token(client)}"}

    patched = await client.patch(f"/users/{user_id}", json={"is_active": False}, headers=admin)
    assert patched.status_code == HTTP_200_OK
    assert patched.json()["is_active"] is False

    # Existing token is revoked, and a fresh login is refused.
    assert (await client.get("/me", headers=auth)).status_code == HTTP_401_UNAUTHORIZED
    relogin = await client.post("/login", json={"email": "bob@b.com", "password": "S3cret!!"})
    assert relogin.status_code == HTTP_401_UNAUTHORIZED

    # Re-enabling restores login.
    await client.patch(f"/users/{user_id}", json={"is_active": True}, headers=admin)
    assert (
        await client.post("/login", json={"email": "bob@b.com", "password": "S3cret!!"})
    ).status_code == HTTP_200_OK


async def test_non_admin_cannot_disable_users(client: AsyncTestClient) -> None:
    await _signup(client, "bob@b.com", "S3cret!!")
    token = (
        await client.post("/login", json={"email": "bob@b.com", "password": "S3cret!!"})
    ).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    user_id = (await client.get("/me", headers=auth)).json()["id"]
    # bob (non-admin) tries to disable himself via the admin endpoint → 403.
    resp = await client.patch(f"/users/{user_id}", json={"is_active": False}, headers=auth)
    assert resp.status_code == 403


async def test_admin_can_promote_and_demote_platform_admin(client: AsyncTestClient) -> None:
    await _signup(client, "bob@b.com", "S3cret!!")
    token = (
        await client.post("/login", json={"email": "bob@b.com", "password": "S3cret!!"})
    ).json()["access_token"]
    bob = {"Authorization": f"Bearer {token}"}
    user_id = (await client.get("/me", headers=bob)).json()["id"]
    admin = {"Authorization": f"Bearer {await _admin_token(client)}"}
    assert (await client.get("/me", headers=bob)).json()["is_admin"] is False

    promoted = await client.patch(f"/users/{user_id}/admin", json={"is_admin": True}, headers=admin)
    assert promoted.status_code == HTTP_200_OK
    assert promoted.json()["is_admin"] is True
    # Effective immediately on bob's existing token — the role is read live from the
    # DB per request, not carried in the JWT.
    assert (await client.get("/me", headers=bob)).json()["is_admin"] is True

    demoted = await client.patch(f"/users/{user_id}/admin", json={"is_admin": False}, headers=admin)
    assert demoted.status_code == HTTP_200_OK
    assert demoted.json()["is_admin"] is False
    assert (await client.get("/me", headers=bob)).json()["is_admin"] is False


async def test_non_admin_cannot_change_admin_role(client: AsyncTestClient) -> None:
    await _signup(client, "bob@b.com", "S3cret!!")
    token = (
        await client.post("/login", json={"email": "bob@b.com", "password": "S3cret!!"})
    ).json()["access_token"]
    bob = {"Authorization": f"Bearer {token}"}
    user_id = (await client.get("/me", headers=bob)).json()["id"]
    # bob tries to promote himself → 403.
    resp = await client.patch(f"/users/{user_id}/admin", json={"is_admin": True}, headers=bob)
    assert resp.status_code == 403


async def test_admin_cannot_change_own_admin_role(client: AsyncTestClient) -> None:
    admin = {"Authorization": f"Bearer {await _admin_token(client)}"}
    admin_id = (await client.get("/me", headers=admin)).json()["id"]
    # Self-demotion is a lockout footgun — refused even for a real admin.
    resp = await client.patch(f"/users/{admin_id}/admin", json={"is_admin": False}, headers=admin)
    assert resp.status_code == 403


async def test_logout_revokes_existing_tokens(client: AsyncTestClient) -> None:
    await _signup(client, "alice@b.com", "S3cret!!")
    token = (
        await client.post("/login", json={"email": "alice@b.com", "password": "S3cret!!"})
    ).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    assert (await client.get("/me", headers=auth)).status_code == HTTP_200_OK

    logout = await client.post("/logout", headers=auth)
    assert logout.status_code in (200, 204)

    # The same token is now revoked.
    assert (await client.get("/me", headers=auth)).status_code == HTTP_401_UNAUTHORIZED

    # A fresh login still works.
    new_token = (
        await client.post("/login", json={"email": "alice@b.com", "password": "S3cret!!"})
    ).json()["access_token"]
    after = await client.get("/me", headers={"Authorization": f"Bearer {new_token}"})
    assert after.status_code == HTTP_200_OK


async def test_admin_can_login_with_master_key(client: AsyncTestClient) -> None:
    # The bootstrapped admin's password IS the master key.
    resp = await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    assert resp.status_code == HTTP_200_OK
    token = resp.json()["access_token"]
    me = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["email"] == ADMIN_EMAIL
    assert me.json()["is_admin"] is True
