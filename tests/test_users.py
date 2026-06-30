"""Integration tests for users: bootstrap, invites, signup."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from litestar import Litestar
from litestar.status_codes import (
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
)
from litestar.testing import AsyncTestClient

from litestar_test.app import create_app
from litestar_test.config import Settings

MASTER_KEY = "master-secret"


def _db_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'users.db'}"


def _settings(tmp_path: Path, master: str | None) -> Settings:
    # Explicit Settings keep tests isolated from the developer's env / .env file.
    return Settings(
        database_url=_db_url(tmp_path),
        admin_email="admin@example.com",
        master_key=master,
        jwt_secret="test-secret-key-0123456789-abcdefghij",
        salt_key="test-salt-key",
    )


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    app = create_app(_settings(tmp_path, MASTER_KEY))
    async with AsyncTestClient(app=app) as test_client:
        yield test_client


def _flatten(exc: BaseException) -> list[BaseException]:
    """Walk ExceptionGroups and __cause__/__context__ chains into a flat list."""
    seen: list[BaseException] = []
    stack = [exc]
    while stack:
        current = stack.pop()
        if current is None or current in seen:
            continue
        seen.append(current)
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        if current.__cause__:
            stack.append(current.__cause__)
        if current.__context__:
            stack.append(current.__context__)
    return seen


async def test_startup_fails_when_empty_and_no_master_key(tmp_path: Path) -> None:
    from litestar_test.domain.exceptions import MasterKeyMissing

    app = create_app(_settings(tmp_path, master=None))
    with pytest.raises(BaseException) as exc_info:
        async with AsyncTestClient(app=app):
            pass
    assert any(isinstance(e, MasterKeyMissing) for e in _flatten(exc_info.value))


async def test_bootstrap_creates_admin(client: AsyncTestClient, tmp_path: Path) -> None:
    # Query the same DB file via an independent engine (avoids AA state-key suffixing).
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from litestar_test.infrastructure.persistence.orm import UserModel

    engine = create_async_engine(_db_url(tmp_path))
    try:
        async with engine.connect() as conn:
            admins = (await conn.execute(select(UserModel.__table__))).all()
    finally:
        await engine.dispose()
    assert len(admins) == 1
    row = admins[0]._mapping
    assert row["email"] == "admin@example.com"
    assert row["is_admin"] is True


async def _admin_token(client: AsyncTestClient) -> str:
    resp = await client.post("/login", json={"email": "admin@example.com", "password": MASTER_KEY})
    return resp.json()["access_token"]


async def _new_invite(client: AsyncTestClient) -> str:
    token = await _admin_token(client)
    resp = await client.post("/invites", headers={"Authorization": f"Bearer {token}"})
    return resp.json()["token"]


async def test_invite_creation_requires_auth(client: AsyncTestClient) -> None:
    assert (await client.post("/invites")).status_code == HTTP_401_UNAUTHORIZED


async def test_invite_creation_rejects_non_admin(client: AsyncTestClient) -> None:
    invite = await _new_invite(client)
    await client.post(
        "/signup",
        json={"invite_token": invite, "email": "user@b.com", "password": "Passw0rd!"},
    )
    user_token = (
        await client.post("/login", json={"email": "user@b.com", "password": "Passw0rd!"})
    ).json()["access_token"]
    resp = await client.post("/invites", headers={"Authorization": f"Bearer {user_token}"})
    assert resp.status_code == HTTP_403_FORBIDDEN


async def test_invite_creation_with_admin_token(client: AsyncTestClient) -> None:
    token = await _admin_token(client)
    resp = await client.post("/invites", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == HTTP_201_CREATED
    assert resp.json()["token"]


async def test_signup_requires_valid_invite(client: AsyncTestClient) -> None:
    resp = await client.post(
        "/signup",
        json={"invite_token": "nope", "email": "a@b.com", "password": "Passw0rd!"},
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST


async def test_signup_rejects_weak_password(client: AsyncTestClient) -> None:
    token = await _new_invite(client)
    resp = await client.post(
        "/signup",
        json={"invite_token": token, "email": "weak@b.com", "password": "weak"},
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST


async def test_signup_with_invite_then_token_is_single_use(
    client: AsyncTestClient,
) -> None:
    token = await _new_invite(client)
    first = await client.post(
        "/signup",
        json={"invite_token": token, "email": "alice@b.com", "password": "Passw0rd!"},
    )
    assert first.status_code == HTTP_201_CREATED
    assert first.json()["email"] == "alice@b.com"
    assert first.json()["is_admin"] is False

    # Same token cannot be reused.
    second = await client.post(
        "/signup",
        json={"invite_token": token, "email": "bob@b.com", "password": "Passw0rd!"},
    )
    assert second.status_code == HTTP_400_BAD_REQUEST


async def test_signup_duplicate_email_is_non_revealing(client: AsyncTestClient) -> None:
    token1 = await _new_invite(client)
    token2 = await _new_invite(client)
    ok = await client.post(
        "/signup",
        json={"invite_token": token1, "email": "dup@b.com", "password": "Passw0rd!"},
    )
    assert ok.status_code == HTTP_201_CREATED
    dup = await client.post(
        "/signup",
        json={"invite_token": token2, "email": "dup@b.com", "password": "Passw0rd!"},
    )
    # A duplicate email must not be distinguishable from a generic failure: same
    # 400 as other client errors, and the email is never echoed back.
    assert dup.status_code == HTTP_400_BAD_REQUEST
    assert "dup@b.com" not in dup.text


async def test_signup_normalizes_email(client: AsyncTestClient) -> None:
    token = await _new_invite(client)
    resp = await client.post(
        "/signup",
        json={"invite_token": token, "email": "  MiXeD@B.CoM ", "password": "Passw0rd!"},
    )
    assert resp.status_code == HTTP_201_CREATED
    assert resp.json()["email"] == "mixed@b.com"


def test_app_imports_and_routes() -> None:
    app = create_app()
    assert isinstance(app, Litestar)
