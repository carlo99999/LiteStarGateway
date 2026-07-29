"""SQLAlchemy adapter implementing the `BudgetAlertStateRepository` port."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import BudgetAlertState, BudgetWindow, PendingBudgetAlert
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.domain.ports.notification_channel import ChannelResolver
from litestar_gateway.infrastructure.persistence.orm import (
    BudgetAlertStateModel,
    PendingBudgetAlertModel,
)

logger = logging.getLogger("litestar_gateway.budget_alerts")

# Mirrors usage_repository.MAX_RECONCILE_ATTEMPTS: after this many failed
# delivery attempts a pending alert is quarantined (stays in the table for
# inspection but is no longer selected), so a permanently-failing target
# can't occupy the oldest-first batch forever and starve newer alerts.
MAX_DISPATCH_ATTEMPTS = 10

# How long a dispatcher owns a claimed row. Long enough that a normal
# webhook/email round-trip finishes inside it, short enough that a worker
# killed mid-delivery frees the row without operator action. Recovery is the
# whole point of a lease rather than a boolean flag.
DISPATCH_LEASE_SECONDS = 300


class SQLAlchemyBudgetAlertStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fired_thresholds(
        self, team_id: UUID, window: BudgetWindow, period_start: datetime
    ) -> set[int]:
        rows = await self._session.scalars(
            select(BudgetAlertStateModel.threshold).where(
                BudgetAlertStateModel.team_id == team_id,
                BudgetAlertStateModel.window == window.value,
                BudgetAlertStateModel.period_start == period_start,
            )
        )
        return set(rows.all())

    async def record_fired(
        self,
        team_id: UUID,
        window: BudgetWindow,
        period_start: datetime,
        threshold: int,
    ) -> BudgetAlertState | None:
        row = BudgetAlertStateModel(
            id=uuid4(),
            team_id=team_id,
            window=window.value,
            period_start=period_start,
            threshold=threshold,
            fired_at=datetime.now(UTC),
        )
        self._session.add(row)
        try:
            await self._session.commit()
        except IntegrityError:
            # Concurrent settlement (this replica or another) already recorded
            # this exact dedup key — the unique constraint made the loser's
            # insert a conflict rather than a duplicate row. Not an error: the
            # threshold is fired either way.
            await self._session.rollback()
            return None
        await self._session.refresh(row)
        return row.to_entity()

    async def enqueue_alert(self, alert: PendingBudgetAlert) -> None:
        """Outbox row on its own. Production settlement never calls this — it
        goes through `record_fired_and_enqueue` so the dedup row and this row
        share one transaction (ISSUE-026); kept for seeding and for the
        dispatch tests."""
        self._session.add(self._outbox_row(alert))
        await self._session.commit()

    @staticmethod
    def _outbox_row(alert: PendingBudgetAlert) -> PendingBudgetAlertModel:
        return PendingBudgetAlertModel(
            id=alert.id,
            team_id=alert.team_id,
            window=alert.window.value,
            period_start=alert.period_start,
            threshold=alert.threshold,
            spend=alert.spend,
            limit_cost=alert.limit_cost,
        )

    async def record_fired_and_enqueue(self, alert: PendingBudgetAlert) -> BudgetAlertState | None:
        """Record the dedup key and queue its delivery in ONE transaction.

        Two commits used to be two failure windows (ISSUE-026): a crash or
        error between them left a threshold marked fired with no outbox row,
        and every later evaluation skipped it as already-fired — the alert was
        lost permanently. Inserting both rows under a single commit makes
        "fired" and "queued" the same durable fact.

        Returns `None` when a concurrent settlement already recorded this exact
        dedup key: the unique constraint makes the loser's insert a conflict,
        and the rollback discards its outbox row too, so the alert is queued
        exactly once."""
        state = BudgetAlertStateModel(
            id=uuid4(),
            team_id=alert.team_id,
            window=alert.window.value,
            period_start=alert.period_start,
            threshold=alert.threshold,
            fired_at=datetime.now(UTC),
        )
        self._session.add(state)
        self._session.add(self._outbox_row(alert))
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            return None
        await self._session.refresh(state)
        return state.to_entity()

    async def pending_alerts(self, *, limit: int = DEFAULT_PAGE_SIZE) -> list[PendingBudgetAlert]:
        rows = await self._session.scalars(
            select(PendingBudgetAlertModel)
            .order_by(PendingBudgetAlertModel.created_at)
            .limit(limit)
        )
        return [row.to_entity() for row in rows.all()]

    async def recent_fired(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE
    ) -> list[BudgetAlertState]:
        """Most-recently fired alerts for a team, newest-first — the
        read-model behind the console's 'recent alerts' list (Plan 07 Phase 3,
        design doc §8). Reads the dedup ledger (`budget_alert_state`), which is
        the durable record of what fired and when, independent of whether the
        outbox row has since been delivered and deleted."""
        rows = await self._session.scalars(
            select(BudgetAlertStateModel)
            .where(BudgetAlertStateModel.team_id == team_id)
            .order_by(BudgetAlertStateModel.fired_at.desc())
            .limit(limit)
        )
        return [row.to_entity() for row in rows.all()]

    async def quarantined_alerts(
        self, *, limit: int = DEFAULT_PAGE_SIZE
    ) -> list[PendingBudgetAlert]:
        rows = await self._session.scalars(
            select(PendingBudgetAlertModel)
            .where(PendingBudgetAlertModel.attempts >= MAX_DISPATCH_ATTEMPTS)
            .order_by(PendingBudgetAlertModel.created_at)
            .limit(limit)
        )
        return [row.to_entity() for row in rows]

    async def requeue(self, alert_id: UUID) -> bool:
        """Reset one quarantined row so the next drain retries it.

        The `attempts >= MAX_DISPATCH_ATTEMPTS` predicate is part of the UPDATE,
        not a separate read: a row that is merely mid-retry must not have its
        lease cleared underneath the dispatcher currently holding it.
        """
        # Any: the async execute() is typed Result, but at runtime it is a
        # CursorResult exposing rowcount.
        result: Any = await self._session.execute(
            update(PendingBudgetAlertModel)
            .where(
                PendingBudgetAlertModel.id == alert_id,
                PendingBudgetAlertModel.attempts >= MAX_DISPATCH_ATTEMPTS,
            )
            # `last_error` is kept deliberately: after a replay an operator still
            # needs to know what went wrong the first ten times.
            .values(attempts=0, claimed_until=None)
        )
        await self._session.commit()
        return bool(result.rowcount)

    async def dispatch_pending(
        self, resolve_channels: ChannelResolver, *, limit: int = DEFAULT_PAGE_SIZE
    ) -> int:
        """Drain up to `limit` pending alerts oldest-first (Plan 07 Phase 2,
        per-team resolution added in Phase 3): for each row, resolve the
        channel(s) for its OWNING TEAM via `resolve_channels`, dispatch through
        every resolved channel, delete the row on success, or bump
        attempts/last_error and leave it queued for retry on failure. Mirrors
        `SQLAlchemyUsageRepository.reconcile_pending`'s shape and
        poison-quarantine policy, reusing the `attempts`/`last_error` columns
        Phase 1 reserved.

        A row whose team resolves to NO channels is skipped untouched (not
        marked failed) — it simply waits for a channel to be configured, the
        same no-op semantics Phase 2 gave an empty platform channel list.

        A row is retried in FULL (all its resolved channels re-run) on any
        single channel's failure — this is a deliberate, consistent extension
        of Phase 2's same simplification to the multi-channel (webhook + email)
        case rather than tracking per-channel delivery state. A partial
        delivery (e.g. webhook succeeds, email fails) therefore re-delivers to
        the already-succeeded channel on the next attempt; accepted for v1 as
        the outbox's existing at-least-once posture, not a new gap."""
        now = datetime.now(UTC)
        pending = (
            await self._session.scalars(
                select(PendingBudgetAlertModel)
                .where(
                    PendingBudgetAlertModel.attempts < MAX_DISPATCH_ATTEMPTS,
                    or_(
                        PendingBudgetAlertModel.claimed_until.is_(None),
                        PendingBudgetAlertModel.claimed_until < now,
                    ),
                )
                .order_by(PendingBudgetAlertModel.created_at)
                .limit(limit)
            )
        ).all()
        # Snapshot everything needed for the whole batch up front: `rollback()`
        # expires every ORM instance in the session, not just the row that
        # failed, so touching a later row's still-ORM-bound attributes after
        # an earlier row's rollback would force a synchronous re-fetch (and
        # blow up under the async engine). Deleting by id (below) means we
        # never need the ORM instance itself again.
        batch = [(row.id, row.attempts, row.to_entity()) for row in pending]
        delivered = 0
        for row_id, attempts, alert in batch:
            if not await self._claim(row_id, now):
                continue  # another dispatcher owns this row
            try:
                # Resolve inside the per-row try so a bad resolve (e.g. a
                # stored URL that fails channel construction) marks just that
                # row failed rather than killing the whole batch.
                channels = await resolve_channels(alert)
                if not channels:
                    # Nowhere to deliver yet: leave the row queued AND
                    # unclaimed. Holding the lease would mean that configuring
                    # a channel is followed by a delivery blackout until it
                    # expires — and with no attempt recorded, nothing would
                    # tell an operator why (ISSUE-031).
                    await self._release_claim(row_id)
                    continue
                for channel in channels:
                    await channel.send(alert)
                await self._session.execute(
                    delete(PendingBudgetAlertModel).where(PendingBudgetAlertModel.id == row_id)
                )
                await self._session.commit()
                delivered += 1
            except Exception as exc:  # one bad row must not stop the batch
                await self._session.rollback()
                await self._mark_failed_attempt(row_id, attempts + 1, exc)
        return delivered

    async def _release_claim(self, row_id: UUID) -> None:
        """Give the lease back without touching `attempts`: this dispatcher had
        nothing to do with the row, so the next drain must be able to pick it up
        immediately."""
        try:
            await self._session.execute(
                update(PendingBudgetAlertModel)
                .where(PendingBudgetAlertModel.id == row_id)
                .values(claimed_until=None)
            )
            await self._session.commit()
        except Exception:  # the lease expires on its own; never break the batch
            await self._session.rollback()
            logger.warning("failed to release pending budget alert claim", exc_info=True)

    async def _claim(self, row_id: UUID, now: datetime) -> bool:
        """Take ownership of one pending row, atomically.

        A conditional UPDATE is a compare-and-swap the database serializes for
        us: concurrent dispatchers both try it, the second re-evaluates the
        predicate after the first commits and matches nothing. Portable across
        PostgreSQL and SQLite, unlike `FOR UPDATE SKIP LOCKED`, and unlike the
        previous select-send-delete it closes the window in which two replicas
        both delivered the same alert before either deleted the row
        (ISSUE-026). An expired lease is claimable again, so a dispatcher that
        dies mid-delivery does not strand the alert.

        Note the deliberate trade: a claim whose holder dies mid-send can be
        retried once the lease expires, so delivery stays at-least-once
        (unchanged) while ceasing to be at-least-once *per replica*."""
        # Any: the async execute() is typed Result, but at runtime it is a
        # CursorResult exposing rowcount.
        try:
            result: Any = await self._session.execute(
                update(PendingBudgetAlertModel)
                .where(
                    PendingBudgetAlertModel.id == row_id,
                    or_(
                        PendingBudgetAlertModel.claimed_until.is_(None),
                        PendingBudgetAlertModel.claimed_until < now,
                    ),
                )
                .values(claimed_until=now + timedelta(seconds=DISPATCH_LEASE_SECONDS))
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            logger.warning("failed to claim pending budget alert", exc_info=True)
            return False
        return bool(result.rowcount)

    async def _mark_failed_attempt(self, row_id: UUID, attempts: int, exc: Exception) -> None:
        """Failure bookkeeping for one pending alert: count the attempt and
        keep the last error. At MAX_DISPATCH_ATTEMPTS the row stops being
        selected (quarantined) — escalate to ERROR so an operator resolves
        the permanently-failing target by hand."""
        try:
            await self._session.execute(
                update(PendingBudgetAlertModel)
                .where(PendingBudgetAlertModel.id == row_id)
                # Release the claim too: this dispatcher is done with the row,
                # so the next drain may retry it without waiting out the lease.
                .values(attempts=attempts, last_error=repr(exc)[:500], claimed_until=None)
            )
            await self._session.commit()
        except Exception:  # bookkeeping is best-effort; the row stays selectable
            await self._session.rollback()
            logger.warning("failed to record alert dispatch attempt", exc_info=True)
            return
        if attempts >= MAX_DISPATCH_ATTEMPTS:
            logger.error(
                "pending budget alert quarantined after %d failed attempts: id=%s",
                attempts,
                row_id,
                exc_info=exc,
            )
        else:
            logger.warning(
                "failed to dispatch pending budget alert (attempt %d/%d): id=%s",
                attempts,
                MAX_DISPATCH_ATTEMPTS,
                row_id,
                exc_info=exc,
            )
