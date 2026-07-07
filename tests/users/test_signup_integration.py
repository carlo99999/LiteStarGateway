"""Integration tests for users: bootstrap, invites, signup."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from litestar import Litestar
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

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
    from litestar_gateway.domain.exceptions import MasterKeyMissing

    app = create_app(_settings(tmp_path, master=None))
    with pytest.raises(BaseException) as exc_info:
        async with AsyncTestClient(app=app):
            pass
    assert any(isinstance(e, MasterKeyMissing) for e in _flatten(exc_info.value))


async def test_bootstrap_creates_admin(client: AsyncTestClient, tmp_path: Path) -> None:
    # Query the same DB file via an independent engine (avoids AA state-key suffixing).
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from litestar_gateway.infrastructure.persistence.orm import UserModel

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


async def _register(client: AsyncTestClient, email: str, password: str) -> None:
    invite = await _new_invite(client)
    await client.post(
        "/signup", json={"invite_token": invite, "email": email, "password": password}
    )


async def test_admin_password_reset_flow(client: AsyncTestClient) -> None:
    admin = await _admin_token(client)
    await _register(client, "u@b.com", "OldPassw0rd!")

    # Admin issues a reset token for the user.
    issued = await client.post(
        "/password-resets", json={"email": "u@b.com"}, headers={"Authorization": f"Bearer {admin}"}
    )
    assert issued.status_code == HTTP_201_CREATED
    token = issued.json()["token"]

    # User redeems it with their own new password.
    redeemed = await client.post(
        "/reset-password", json={"reset_token": token, "new_password": "NewPassw0rd!"}
    )
    assert redeemed.status_code in (200, 204)

    # New password works; old one no longer does.
    assert (
        await client.post("/login", json={"email": "u@b.com", "password": "NewPassw0rd!"})
    ).status_code == HTTP_200_OK
    assert (
        await client.post("/login", json={"email": "u@b.com", "password": "OldPassw0rd!"})
    ).status_code == HTTP_401_UNAUTHORIZED


async def test_reset_password_failure_does_not_burn_the_token(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Redeeming a reset is one unit of work: if the password write fails after
    # the token was consumed, the whole transaction rolls back — the user can
    # retry with the same token instead of being stranded (token burned, old
    # password still in place) until an admin issues a new one.
    admin = await _admin_token(client)
    await _register(client, "u@b.com", "OldPassw0rd!")
    token = (
        await client.post(
            "/password-resets",
            json={"email": "u@b.com"},
            headers={"Authorization": f"Bearer {admin}"},
        )
    ).json()["token"]

    from litestar_gateway.infrastructure.persistence import user_repository

    async def boom(self: object, user_id: object, password_hash: str) -> None:
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(user_repository.SQLAlchemyUserRepository, "set_password", boom)
    failed = await client.post(
        "/reset-password", json={"reset_token": token, "new_password": "NewPassw0rd!"}
    )
    assert failed.status_code == 500
    monkeypatch.undo()

    # Same token, second attempt: must still be redeemable.
    redeemed = await client.post(
        "/reset-password", json={"reset_token": token, "new_password": "NewPassw0rd!"}
    )
    assert redeemed.status_code in (200, 204)
    assert (
        await client.post("/login", json={"email": "u@b.com", "password": "NewPassw0rd!"})
    ).status_code == HTTP_200_OK


async def test_password_reset_requires_admin(client: AsyncTestClient) -> None:
    await _register(client, "u@b.com", "Passw0rd!")
    user = (await client.post("/login", json={"email": "u@b.com", "password": "Passw0rd!"})).json()[
        "access_token"
    ]
    resp = await client.post(
        "/password-resets", json={"email": "u@b.com"}, headers={"Authorization": f"Bearer {user}"}
    )
    assert resp.status_code == HTTP_403_FORBIDDEN


async def test_reset_password_invalid_token_is_non_revealing(client: AsyncTestClient) -> None:
    resp = await client.post(
        "/reset-password", json={"reset_token": "nope", "new_password": "NewPassw0rd!"}
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST


def test_app_imports_and_routes() -> None:
    app = create_app()
    assert isinstance(app, Litestar)
