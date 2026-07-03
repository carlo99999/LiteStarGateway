"""Per-account login lockout: repeated failures lock the account temporarily.

The per-IP auth rate limit doesn't stop a *distributed* password-guessing
attack against one account (many IPs, few attempts each). A per-email failure
counter does: after MAX_FAILED_LOGINS wrong passwords the account is locked
for LOCKOUT_DURATION — even the correct password is rejected — and the
response stays the generic 401 (no lock/enumeration disclosure). A successful
login resets the counter. State lives on the user row, so it holds across
workers and restarts and SSO logins are unaffected.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_401_UNAUTHORIZED
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.application.user_service import MAX_FAILED_LOGINS
from litestar_gateway.config import Settings

MASTER_KEY = "master-secret"
ADMIN_EMAIL = "admin@example.com"
USER_EMAIL = "alice@b.com"
USER_PASSWORD = "S3cret!!"  # pragma: allowlist secret


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'lock.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


async def _signup(client: AsyncTestClient) -> None:
    admin = (
        await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    ).json()["access_token"]
    token = (await client.post("/invites", headers={"Authorization": f"Bearer {admin}"})).json()[
        "token"
    ]
    resp = await client.post(
        "/signup",
        json={"invite_token": token, "email": USER_EMAIL, "password": USER_PASSWORD},
    )
    assert resp.status_code == 201


async def _login(client: AsyncTestClient, password: str):
    return await client.post("/login", json={"email": USER_EMAIL, "password": password})


async def test_account_locks_after_max_failures(client: AsyncTestClient) -> None:
    await _signup(client)
    for _ in range(MAX_FAILED_LOGINS):
        assert (await _login(client, "wrong-password")).status_code == HTTP_401_UNAUTHORIZED
    # Locked: even the correct password is rejected now.
    assert (await _login(client, USER_PASSWORD)).status_code == HTTP_401_UNAUTHORIZED


async def test_lock_response_is_indistinguishable(client: AsyncTestClient) -> None:
    # A locked account and a plain wrong password return the same body — the
    # lock must not be observable (no enumeration/lock-state oracle).
    await _signup(client)
    wrong = await _login(client, "wrong-password")
    for _ in range(MAX_FAILED_LOGINS):
        await _login(client, "wrong-password")
    locked = await _login(client, USER_PASSWORD)
    assert locked.status_code == wrong.status_code == HTTP_401_UNAUTHORIZED
    assert locked.json() == wrong.json()


async def test_successful_login_resets_the_counter(client: AsyncTestClient) -> None:
    await _signup(client)
    for _ in range(MAX_FAILED_LOGINS - 1):
        await _login(client, "wrong-password")
    assert (await _login(client, USER_PASSWORD)).status_code == HTTP_200_OK  # resets
    # A fresh full window of failures is needed to lock now.
    for _ in range(MAX_FAILED_LOGINS - 1):
        await _login(client, "wrong-password")
    assert (await _login(client, USER_PASSWORD)).status_code == HTTP_200_OK


async def test_lock_expires(client: AsyncTestClient, tmp_path: Path) -> None:
    await _signup(client)
    for _ in range(MAX_FAILED_LOGINS):
        await _login(client, "wrong-password")
    assert (await _login(client, USER_PASSWORD)).status_code == HTTP_401_UNAUTHORIZED

    # Simulate the lock window passing (backdate locked_until in the DB).
    conn = sqlite3.connect(str(tmp_path / "lock.db"))
    try:
        conn.execute(
            "UPDATE user_account SET locked_until = '2020-01-01 00:00:00' WHERE email = ?",
            (USER_EMAIL,),
        )
        conn.commit()
    finally:
        conn.close()

    assert (await _login(client, USER_PASSWORD)).status_code == HTTP_200_OK


async def test_unknown_email_still_generic_401(client: AsyncTestClient) -> None:
    body = {"email": "ghost@b.com", "password": "whatever"}  # pragma: allowlist secret
    resp = await client.post("/login", json=body)
    assert resp.status_code == HTTP_401_UNAUTHORIZED
