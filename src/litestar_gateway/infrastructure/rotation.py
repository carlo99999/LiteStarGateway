"""Daily key rotation, driven from the app lifespan.

When enabled, a background task sleeps until the configured UTC time each day and
rotates both keyrings:
  * credentials — a new data key becomes active, all credentials are re-encrypted
    to it, and older keys are retired (kept for readability, no longer active);
  * JWT — a new signing key becomes active, and keys older than the token TTL are
    dropped (no valid token could still need them).

The task is fail-safe: a failed rotation is logged and the loop continues.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from litestar import Litestar

from litestar_test.config import Settings
from litestar_test.domain.ports import DistributedLock
from litestar_test.infrastructure.keyring import Keyring
from litestar_test.infrastructure.locks import build_distributed_lock
from litestar_test.infrastructure.persistence.credential_repository import (
    SQLAlchemyCredentialRepository,
)
from litestar_test.infrastructure.persistence.database import Database
from litestar_test.infrastructure.persistence.secret_key_repository import (
    SQLAlchemySecretKeyRepository,
)
from litestar_test.infrastructure.web.session.jwt import ACCESS_TOKEN_TTL

logger = logging.getLogger("litestar_test.rotation")

# Only one replica should rotate at a time; hold a cross-replica lock while doing
# so. TTL comfortably exceeds a normal rotation but stays well under the daily
# interval, so a crashed holder can't block the next day's rotation.
_ROTATION_LOCK_NAME = "litestar-gateway:key-rotation"
_ROTATION_LOCK_TTL = timedelta(minutes=15)


async def guarded_rotate(lock: DistributedLock, rotate: Callable[[], Awaitable[None]]) -> bool:
    """Run `rotate` only if the cross-replica lock is acquired; return whether it ran."""
    async with lock.hold(_ROTATION_LOCK_NAME, ttl=_ROTATION_LOCK_TTL) as acquired:
        if acquired:
            await rotate()
        else:
            logger.info("key rotation skipped: another instance holds the lock")
        return acquired


class RotationService:
    def __init__(
        self,
        keyring: Keyring,
        credentials: SQLAlchemyCredentialRepository,
        jwt_max_age: timedelta,
    ) -> None:
        self._keyring = keyring
        self._credentials = credentials
        self._jwt_max_age = jwt_max_age

    async def rotate_all(self) -> None:
        # Credentials: new active key, re-encrypt everything, retire the rest.
        await self._keyring.new_credential_key()
        await self._credentials.reencrypt_all()
        await self._keyring.retire_old_credential_keys()
        # JWT: new active key, prune keys older than the token TTL.
        await self._keyring.rotate_jwt(self._jwt_max_age)


def seconds_until(target_hhmm: str, now: datetime) -> float:
    """Seconds from `now` until the next UTC occurrence of "HH:MM"."""
    hour, minute = (int(part) for part in target_hhmm.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def make_rotation_scheduler(database: Database, settings: Settings):
    """Return a Litestar lifespan that runs daily rotation when enabled."""

    lock = build_distributed_lock(settings)

    async def _rotate_once(app: Litestar) -> None:
        async def _rotate() -> None:
            session_maker = app.state[database.config.session_maker_app_state_key]
            async with session_maker() as session:
                keyring = Keyring(
                    SQLAlchemySecretKeyRepository(session), settings.salt_key, settings.jwt_secret
                )
                credentials = SQLAlchemyCredentialRepository(session, keyring)
                await RotationService(keyring, credentials, ACCESS_TOKEN_TTL).rotate_all()

        # With multiple replicas, only the lock holder rotates; the rest skip.
        await guarded_rotate(lock, _rotate)

    async def _loop(app: Litestar) -> None:
        while True:
            await asyncio.sleep(seconds_until(settings.rotation_time, datetime.now(UTC)))
            try:
                await _rotate_once(app)
                logger.info("key rotation completed")
            except Exception:  # never let a failure kill the loop
                logger.exception("key rotation failed")

    @asynccontextmanager
    async def lifespan(app: Litestar) -> AsyncIterator[None]:
        if not settings.rotation_enabled:
            yield
            return
        task = asyncio.create_task(_loop(app))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return lifespan
