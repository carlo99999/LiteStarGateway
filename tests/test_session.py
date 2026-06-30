"""Integration tests for login / JWT sessions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_401_UNAUTHORIZED
from litestar.testing import AsyncTestClient

from litestar_test.app import create_app
from litestar_test.config import Settings

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
    token = (await client.post("/invites", headers={"Authorization": f"Bearer {admin}"})).json()[
        "token"
    ]
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


async def test_admin_can_login_with_master_key(client: AsyncTestClient) -> None:
    # The bootstrapped admin's password IS the master key.
    resp = await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    assert resp.status_code == HTTP_200_OK
    token = resp.json()["access_token"]
    me = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["email"] == ADMIN_EMAIL
    assert me.json()["is_admin"] is True
