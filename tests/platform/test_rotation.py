"""Tests for key rotation: envelope re-encryption, JWT keyring, schedule math."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.domain.entities import Credential, KeyPurpose, Provider
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.credential_repository import (
    SQLAlchemyCredentialRepository,
)
from litestar_gateway.infrastructure.persistence.orm import CredentialModel
from litestar_gateway.infrastructure.persistence.secret_key_repository import (
    SQLAlchemySecretKeyRepository,
)
from litestar_gateway.infrastructure.rotation import RotationService, seconds_until

MASTER = "unit-test-salt"
JWT_MASTER = "unit-test-jwt-secret"


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rot.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(base.UUIDAuditBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _keyring(session: AsyncSession) -> Keyring:
    return Keyring(SQLAlchemySecretKeyRepository(session), MASTER, JWT_MASTER)


async def test_credential_rotation_reencrypts_and_stays_readable(session: AsyncSession) -> None:
    keyring = _keyring(session)
    creds = SQLAlchemyCredentialRepository(session, keyring)
    cred = Credential(id=uuid4(), name="c", provider=Provider.OPENAI, created_at=datetime.now(UTC))
    await creds.add(cred, {"api_key": "secret-value"})
    before = await session.get(CredentialModel, cred.id)
    assert before is not None
    original_key_id = before.key_id

    await RotationService(keyring, creds, timedelta(days=7)).rotate_all()
    session.expire_all()

    # Value still decrypts, but it's under a new data key now.
    assert await creds.get_values(cred.id) == {"api_key": "secret-value"}
    after = await session.get(CredentialModel, cred.id)
    assert after is not None
    assert after.key_id != original_key_id

    # Two credential keys exist; the old one is retired (kept for readability).
    keys = await SQLAlchemySecretKeyRepository(session).list_usable(KeyPurpose.CREDENTIAL)
    assert len(keys) == 1  # only the new active key is still "usable"


async def test_jwt_rotation_keeps_recent_keys_for_verification(session: AsyncSession) -> None:
    keyring = _keyring(session)
    await keyring.active_jwt_secret()  # creates the first key
    assert len(await keyring.jwt_verification_secrets()) == 1

    await keyring.rotate_jwt(timedelta(days=7))
    # The recent key is still within the TTL window, so both verify.
    assert len(await keyring.jwt_verification_secrets()) == 2


def test_seconds_until_same_day() -> None:
    now = datetime(2026, 1, 1, 1, 0, 0, tzinfo=UTC)
    assert seconds_until("03:00", now) == 2 * 3600


def test_seconds_until_next_day_when_passed() -> None:
    now = datetime(2026, 1, 1, 5, 0, 0, tzinfo=UTC)
    assert seconds_until("03:00", now) == 22 * 3600


class _FakeLock:
    """Yields a fixed acquired/not-acquired result, to drive guarded_rotate."""

    def __init__(self, acquired: bool) -> None:
        self._acquired = acquired

    @asynccontextmanager
    async def hold(self, name: str, *, ttl: timedelta):  # noqa: ANN201
        yield self._acquired


async def test_guarded_rotate_runs_when_lock_acquired() -> None:
    from litestar_gateway.infrastructure.rotation import guarded_rotate

    ran: list[bool] = []

    async def rotate() -> None:
        ran.append(True)

    did = await guarded_rotate(_FakeLock(True), rotate)
    assert did is True
    assert ran == [True]


async def test_guarded_rotate_skips_when_lock_held_elsewhere() -> None:
    from litestar_gateway.infrastructure.rotation import guarded_rotate

    ran: list[bool] = []

    async def rotate() -> None:
        ran.append(True)

    did = await guarded_rotate(_FakeLock(False), rotate)
    assert did is False
    assert ran == []  # another replica holds the lock → we did not rotate


def _lock_settings(redis_url: str | None):  # noqa: ANN202
    from litestar_gateway.config import Settings

    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        admin_email="admin@example.com",
        master_key="master",
        jwt_secret="dev-jwt",  # pragma: allowlist secret
        salt_key="dev-salt",
        environment="development",
        redis_url=redis_url,
    )


def test_build_distributed_lock_selects_backend() -> None:
    from litestar_gateway.infrastructure.locks import (
        NoOpDistributedLock,
        RedisDistributedLock,
        build_distributed_lock,
    )

    assert isinstance(build_distributed_lock(_lock_settings(None)), NoOpDistributedLock)
    assert isinstance(
        build_distributed_lock(_lock_settings("redis://localhost:6379")), RedisDistributedLock
    )


async def test_noop_lock_always_acquires() -> None:
    from litestar_gateway.infrastructure.locks import NoOpDistributedLock

    async with NoOpDistributedLock().hold("x", ttl=timedelta(seconds=1)) as acquired:
        assert acquired is True


async def test_redis_lock_outage_skips_without_leaking_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # acquire() raising (Redis down) must yield acquired=False and still close
    # the client — previously the exception bubbled before the try, leaking the
    # connection pool and killing that day's guarded run with a traceback.
    import litestar_gateway.infrastructure.locks as locks_module
    from litestar_gateway.infrastructure.locks import RedisDistributedLock

    closed: list[bool] = []

    class FakeLock:
        async def acquire(self) -> bool:
            raise ConnectionError("redis down")

    class FakeRedis:
        @classmethod
        def from_url(cls, url: str) -> FakeRedis:
            return cls()

        def lock(self, name: str, **kwargs: object) -> FakeLock:
            return FakeLock()

        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr(locks_module, "Redis", FakeRedis)

    async with RedisDistributedLock("redis://x").hold("k", ttl=timedelta(seconds=1)) as acquired:
        assert acquired is False  # skip the guarded section, don't crash it
    assert closed == [True]
