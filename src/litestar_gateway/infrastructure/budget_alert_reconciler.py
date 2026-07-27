"""Background delivery worker for the budget-alert outbox (Plan 07 Phase 2).

Mirrors `usage_reconciler.py`'s shape exactly: a lifespan-managed loop that
periodically drains `pending_budget_alert` (Phase 1's outbox) via
`SQLAlchemyBudgetAlertStateRepository.dispatch_pending`, which dispatches each
row through the configured `NotificationChannel`s and deletes it on success.
Fail-safe: an error is logged and the loop continues — a broken webhook, an
SMTP outage, or a bug in one row's dispatch never kills delivery for the rest
of the outbox, and never touches any in-flight inference request (this
worker isn't on that path at all).

Deliberately NOT guarded by the `DistributedLock` port, for the same reason
`usage_reconciler.py` isn't: each row settles as dispatch-then-delete in one
transaction, so two replicas racing the same row are safe (whichever deletes
first wins; the loser's delete of an already-gone row is a no-op)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from litestar import Litestar

from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.notifications.channel_resolver import make_channel_resolver
from litestar_gateway.infrastructure.persistence.budget_alert_state_repository import (
    SQLAlchemyBudgetAlertStateRepository,
)
from litestar_gateway.infrastructure.persistence.budget_repository import (
    SQLAlchemyBudgetRepository,
)
from litestar_gateway.infrastructure.persistence.database import Database

logger = logging.getLogger("litestar_gateway.budget_alerts")

_DISPATCH_INTERVAL_SECONDS = 60
_DISPATCH_BATCH = 200


def make_budget_alert_dispatcher(database: Database, settings: Settings):
    """Return a Litestar lifespan that periodically drains the budget-alert
    outbox, resolving each alert's delivery channel(s) from its owning team's
    budget (Plan 07 Phase 3). Callers should only register this lifespan when
    some delivery capability is configured (see `app.py`'s `_build_lifespan`,
    gated on `settings.budget_alert_delivery_configured`) — otherwise every
    alert resolves to no channels and the outbox is left queuing quietly."""

    async def _dispatch_once(app: Litestar) -> None:
        session_maker = app.state[database.config.session_maker_app_state_key]
        async with session_maker() as session:
            repo = SQLAlchemyBudgetAlertStateRepository(session)
            resolve = make_channel_resolver(SQLAlchemyBudgetRepository(session), settings)
            delivered = await repo.dispatch_pending(resolve, limit=_DISPATCH_BATCH)
            if delivered:
                logger.info("delivered %d pending budget alert(s)", delivered)

    async def _loop(app: Litestar) -> None:
        while True:
            await asyncio.sleep(_DISPATCH_INTERVAL_SECONDS)
            try:
                await _dispatch_once(app)
            except Exception:  # never let a failure kill the loop
                logger.exception("budget alert dispatch failed")

    @asynccontextmanager
    async def lifespan(app: Litestar) -> AsyncGenerator[None]:
        task = asyncio.create_task(_loop(app))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return lifespan
