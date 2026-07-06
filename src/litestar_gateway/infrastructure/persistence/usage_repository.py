"""SQLAlchemy adapter implementing the `UsageRepository` port."""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import ApiKeySpend, UsageAggregate, UsageEvent
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.infrastructure.persistence.orm import PendingUsageEventModel, UsageEventModel

logger = logging.getLogger("litestar_gateway.usage")

# After this many failed reconcile attempts a pending row is quarantined: it
# stays in the table for inspection but is no longer selected, so a poisoned
# row (e.g. its team/key was deleted while it sat in the queue) can't occupy
# the oldest-first batch forever and starve newer events.
MAX_RECONCILE_ATTEMPTS = 10


class SQLAlchemyUsageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, event: UsageEvent) -> None:
        # Stamp the event's own timestamp, not the insert default: budget
        # windows and monthly aggregates must reflect when the spend happened.
        self._session.add(
            UsageEventModel(
                id=event.id,
                team_id=event.team_id,
                api_key_id=event.api_key_id,
                model_id=event.model_id,
                model_name=event.model_name,
                operation=event.operation,
                prompt_tokens=event.prompt_tokens,
                completion_tokens=event.completion_tokens,
                cost=event.cost,
                created_at=event.created_at,
            )
        )
        await self._session.commit()

    async def aggregate(
        self,
        team_id: UUID,
        *,
        model_name: str | None = None,
        api_key_id: UUID | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[UsageAggregate]:
        # One row per model (GROUP BY), but a team's model count is unbounded, so
        # this pages like every other list query.
        query = (
            select(
                UsageEventModel.model_id,
                UsageEventModel.model_name,
                func.coalesce(func.sum(UsageEventModel.prompt_tokens), 0),
                func.coalesce(func.sum(UsageEventModel.completion_tokens), 0),
                func.coalesce(func.sum(UsageEventModel.cost), 0.0),
                func.count(),
            )
            .where(UsageEventModel.team_id == team_id)
            .group_by(UsageEventModel.model_id, UsageEventModel.model_name)
            .order_by(UsageEventModel.model_name)
            .limit(limit)
            .offset(offset)
        )
        if model_name is not None:
            query = query.where(UsageEventModel.model_name == model_name)
        if api_key_id is not None:
            query = query.where(UsageEventModel.api_key_id == api_key_id)

        rows = (await self._session.execute(query)).all()
        return [
            UsageAggregate(
                model_id=row[0],
                model_name=row[1],
                prompt_tokens=int(row[2]),
                completion_tokens=int(row[3]),
                cost=float(row[4]),
                calls=int(row[5]),
            )
            for row in rows
        ]

    async def spend_by_api_key(self, team_id: UUID) -> list[ApiKeySpend]:
        query = (
            select(
                UsageEventModel.api_key_id,
                func.coalesce(func.sum(UsageEventModel.prompt_tokens), 0),
                func.coalesce(func.sum(UsageEventModel.completion_tokens), 0),
                func.coalesce(func.sum(UsageEventModel.cost), 0.0),
                func.count(),
            )
            .where(UsageEventModel.team_id == team_id)
            .group_by(UsageEventModel.api_key_id)
        )
        rows = (await self._session.execute(query)).all()
        return [
            ApiKeySpend(
                api_key_id=row[0],
                prompt_tokens=int(row[1]),
                completion_tokens=int(row[2]),
                cost=float(row[3]),
                calls=int(row[4]),
            )
            for row in rows
        ]

    async def spend_since(self, team_id: UUID, since: datetime) -> float:
        # Hot-path read for the budget gate: an indexed team_id filter + SUM.
        # If this ever gets hot enough to matter, move to a running counter.
        total = await self._session.scalar(
            select(func.coalesce(func.sum(UsageEventModel.cost), 0.0)).where(
                UsageEventModel.team_id == team_id,
                UsageEventModel.created_at >= since,
            )
        )
        # Dead-lettered spend is real cost (already billed upstream) that the
        # reconciler hasn't drained yet — the gate must see it, or a ledger
        # write degradation doubles as a budget-cap bypass window. An event
        # lives in exactly one of the two tables at any commit point (the
        # reconciler inserts + deletes in one transaction), so this never
        # double-counts.
        # Quarantined rows (attempts >= MAX_RECONCILE_ATTEMPTS) are excluded:
        # the reconciler will never drain them into the ledger, so they will
        # never actually bill — counting them would permanently shrink the
        # team's usable budget by a phantom amount for the whole window (M28).
        pending = await self._session.scalar(
            select(func.coalesce(func.sum(PendingUsageEventModel.cost), 0.0)).where(
                PendingUsageEventModel.team_id == team_id,
                PendingUsageEventModel.event_created_at >= since,
                PendingUsageEventModel.attempts < MAX_RECONCILE_ATTEMPTS,
            )
        )
        return float(total or 0.0) + float(pending or 0.0)

    async def enqueue_pending(self, event: UsageEvent) -> None:
        # The request session may be in a failed state after the ledger commit
        # failed; roll back before writing the dead-letter row.
        await self._session.rollback()
        self._session.add(
            PendingUsageEventModel(
                event_id=event.id,
                team_id=event.team_id,
                api_key_id=event.api_key_id,
                model_id=event.model_id,
                model_name=event.model_name,
                operation=event.operation,
                prompt_tokens=event.prompt_tokens,
                completion_tokens=event.completion_tokens,
                cost=event.cost,
                event_created_at=event.created_at,
            )
        )
        await self._session.commit()

    async def reconcile_pending(self, *, limit: int = DEFAULT_PAGE_SIZE) -> int:
        pending = (
            await self._session.scalars(
                select(PendingUsageEventModel)
                .where(PendingUsageEventModel.attempts < MAX_RECONCILE_ATTEMPTS)
                .order_by(PendingUsageEventModel.created_at)
                .limit(limit)
            )
        ).all()
        settled = 0
        for row in pending:
            # Captured up front: after a rollback the ORM row may be expired and
            # refreshing it would need IO of its own.
            row_id, event_id, attempts = row.id, row.event_id, row.attempts
            try:
                # Idempotent: skip the ledger insert if the event already landed.
                if await self._session.get(UsageEventModel, row.event_id) is None:
                    self._session.add(
                        UsageEventModel(
                            id=row.event_id,
                            team_id=row.team_id,
                            api_key_id=row.api_key_id,
                            model_id=row.model_id,
                            model_name=row.model_name,
                            operation=row.operation,
                            prompt_tokens=row.prompt_tokens,
                            completion_tokens=row.completion_tokens,
                            cost=row.cost,
                            # The time the event happened, not the reconcile
                            # time — a drain must not shift spend into the
                            # next budget window.
                            created_at=row.event_created_at,
                        )
                    )
                await self._session.delete(row)
                await self._session.commit()
                settled += 1
            except Exception as exc:  # leave it queued for the next cycle
                await self._session.rollback()
                await self._mark_failed_attempt(row_id, event_id, attempts + 1, exc)
        return settled

    async def _mark_failed_attempt(
        self, row_id: UUID, event_id: UUID, attempts: int, exc: Exception
    ) -> None:
        """Failure bookkeeping for one pending row: count the attempt and keep
        the last error. At MAX_RECONCILE_ATTEMPTS the row stops being selected
        (quarantined) — escalate to ERROR so an operator resolves it by hand."""
        try:
            await self._session.execute(
                update(PendingUsageEventModel)
                .where(PendingUsageEventModel.id == row_id)
                .values(attempts=attempts, last_error=repr(exc)[:500])
            )
            await self._session.commit()
        except Exception:  # bookkeeping is best-effort; the row stays selectable
            await self._session.rollback()
            logger.warning("failed to record reconcile attempt", exc_info=True)
            return
        if attempts >= MAX_RECONCILE_ATTEMPTS:
            logger.error(
                "pending usage event quarantined after %d failed attempts: event=%s",
                attempts,
                event_id,
                exc_info=exc,
            )
        else:
            logger.warning(
                "failed to reconcile pending usage event (attempt %d/%d): event=%s",
                attempts,
                MAX_RECONCILE_ATTEMPTS,
                event_id,
                exc_info=exc,
            )
