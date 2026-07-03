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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_204_NO_CONTENT,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.application.user_service import (
    LOCKOUT_DURATION,
    MAX_FAILED_LOGINS,
    MAX_LOCKOUT_DURATION,
    lockout_duration,
)
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


async def _signup(client: AsyncTestClient) -> str:
    """Create the test user; returns their id."""
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
    return resp.json()["id"]


async def _admin_token(client: AsyncTestClient) -> str:
    resp = await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    return resp.json()["access_token"]


def _db(tmp_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(tmp_path / "lock.db"))


def _set_locked_until(tmp_path: Path, when: datetime) -> None:
    """Backdate the lock in the DB (naive UTC, matching the stored format)."""
    conn = _db(tmp_path)
    try:
        conn.execute(
            "UPDATE user_account SET locked_until = ? WHERE email = ?",
            (when.replace(tzinfo=None).isoformat(sep=" "), USER_EMAIL),
        )
        conn.commit()
    finally:
        conn.close()


def _read_locked_until(tmp_path: Path) -> datetime:
    conn = _db(tmp_path)
    try:
        row = conn.execute(
            "SELECT locked_until FROM user_account WHERE email = ?", (USER_EMAIL,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] is not None
    return datetime.fromisoformat(row[0]).replace(tzinfo=UTC)


async def _lock_account(client: AsyncTestClient) -> None:
    for _ in range(MAX_FAILED_LOGINS):
        assert (await _login(client, "wrong-password")).status_code == HTTP_401_UNAUTHORIZED


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


def test_lockout_duration_escalates_and_is_capped() -> None:
    # Pure escalation curve: base duration doubled per prior consecutive lock
    # cycle, hard-capped so a victim is never locked out for days at a time.
    assert lockout_duration(0) == LOCKOUT_DURATION
    assert lockout_duration(1) == LOCKOUT_DURATION * 2
    assert lockout_duration(2) == LOCKOUT_DURATION * 4
    assert lockout_duration(50) == MAX_LOCKOUT_DURATION


async def test_repeated_lock_cycles_escalate(client: AsyncTestClient, tmp_path: Path) -> None:
    # An attacker who re-locks the account as soon as the lock expires must
    # face a growing window: 20 requests/hour kept the victim locked forever
    # at a flat 15 minutes. Second consecutive cycle → double the duration.
    await _signup(client)
    await _lock_account(client)
    first = _read_locked_until(tmp_path) - datetime.now(UTC)
    assert first <= LOCKOUT_DURATION + timedelta(minutes=1)

    # Lock expires; the attacker immediately forces another cycle.
    _set_locked_until(tmp_path, datetime.now(UTC) - timedelta(minutes=1))
    await _lock_account(client)
    second = _read_locked_until(tmp_path) - datetime.now(UTC)
    assert second > LOCKOUT_DURATION + timedelta(minutes=1)  # escalated
    assert second <= LOCKOUT_DURATION * 2 + timedelta(minutes=1)


async def test_quiet_period_resets_the_escalation(client: AsyncTestClient, tmp_path: Path) -> None:
    # A lock cycle from long ago must not inflate today's lock: after a quiet
    # LOCKOUT_DECAY the escalation restarts from the base duration.
    await _signup(client)
    await _lock_account(client)
    _set_locked_until(tmp_path, datetime.now(UTC) - timedelta(days=2))

    await _lock_account(client)
    duration = _read_locked_until(tmp_path) - datetime.now(UTC)
    assert duration <= LOCKOUT_DURATION + timedelta(minutes=1)  # back to base


async def test_successful_login_resets_the_escalation(
    client: AsyncTestClient, tmp_path: Path
) -> None:
    # The victim logging in successfully proves the account is healthy again —
    # the next lock (if any) starts from the base duration.
    await _signup(client)
    await _lock_account(client)
    _set_locked_until(tmp_path, datetime.now(UTC) - timedelta(minutes=1))
    assert (await _login(client, USER_PASSWORD)).status_code == HTTP_200_OK

    await _lock_account(client)
    duration = _read_locked_until(tmp_path) - datetime.now(UTC)
    assert duration <= LOCKOUT_DURATION + timedelta(minutes=1)


async def test_admin_can_unlock_a_locked_account(client: AsyncTestClient, tmp_path: Path) -> None:
    # The recovery lever: a platform admin lifts the lock at any moment and
    # the correct password works immediately (no waiting out the window).
    user_id = await _signup(client)
    await _lock_account(client)
    assert (await _login(client, USER_PASSWORD)).status_code == HTTP_401_UNAUTHORIZED

    admin = await _admin_token(client)
    resp = await client.delete(
        f"/users/{user_id}/lock", headers={"Authorization": f"Bearer {admin}"}
    )
    assert resp.status_code == HTTP_204_NO_CONTENT
    assert (await _login(client, USER_PASSWORD)).status_code == HTTP_200_OK


async def test_non_admin_cannot_unlock(client: AsyncTestClient, tmp_path: Path) -> None:
    user_id = await _signup(client)
    token = (await _login(client, USER_PASSWORD)).json()["access_token"]
    resp = await client.delete(
        f"/users/{user_id}/lock", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == HTTP_403_FORBIDDEN
