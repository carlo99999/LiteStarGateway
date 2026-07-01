"""Tests for key rotation: envelope re-encryption, JWT keyring, schedule math."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_test.domain.entities import Credential, KeyPurpose, Provider
from litestar_test.infrastructure.keyring import Keyring
from litestar_test.infrastructure.persistence.credential_repository import (
    SQLAlchemyCredentialRepository,
)
from litestar_test.infrastructure.persistence.orm import CredentialModel
from litestar_test.infrastructure.persistence.secret_key_repository import (
    SQLAlchemySecretKeyRepository,
)
from litestar_test.infrastructure.rotation import RotationService, seconds_until

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
