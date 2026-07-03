"""Background reconciler for the usage-billing outbox.

Usage events whose ledger write failed are dead-lettered to `pending_usage_event`.
This periodic task retries them into `usage_event` (idempotent), so a transient
failure under load never silently drops a billing record. Fail-safe: an error is
logged and the loop continues.

Deliberately NOT guarded by the `DistributedLock` port (unlike the rotation
loop): each row settles as check-insert-delete in one transaction, so two
replicas racing the same row are safe — the loser hits the primary-key
conflict, rolls back, and moves on. Running on every replica costs only
redundant work and a little DB contention, in exchange for zero dependence on
Redis for billing convergence. Reach for the lock only if replica count makes
that redundancy measurable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from litestar import Litestar

from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.persistence.database import Database
from litestar_gateway.infrastructure.persistence.usage_repository import SQLAlchemyUsageRepository

logger = logging.getLogger("litestar_gateway.usage")

_RECONCILE_INTERVAL_SECONDS = 60
_RECONCILE_BATCH = 200


def make_usage_reconciler(database: Database, settings: Settings):
    """Return a Litestar lifespan that periodically drains the usage outbox."""

    async def _reconcile_once(app: Litestar) -> None:
        session_maker = app.state[database.config.session_maker_app_state_key]
        async with session_maker() as session:
            settled = await SQLAlchemyUsageRepository(session).reconcile_pending(
                limit=_RECONCILE_BATCH
            )
            if settled:
                logger.info("reconciled %d pending usage event(s)", settled)

    async def _loop(app: Litestar) -> None:
        while True:
            await asyncio.sleep(_RECONCILE_INTERVAL_SECONDS)
            try:
                await _reconcile_once(app)
            except Exception:  # never let a failure kill the loop
                logger.exception("usage reconciliation failed")

    @asynccontextmanager
    async def lifespan(app: Litestar) -> AsyncIterator[None]:
        task = asyncio.create_task(_loop(app))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return lifespan
