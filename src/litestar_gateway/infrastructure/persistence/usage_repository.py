"""SQLAlchemy adapter implementing the `UsageRepository` port."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import ApiKeySpend, UsageAggregate, UsageBucket, UsageEvent
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.infrastructure.persistence.orm import (
    ModelRecord,
    PendingUsageEventModel,
    UsageEventModel,
)

logger = logging.getLogger("litestar_gateway.usage")

# After this many failed reconcile attempts a pending row is quarantined: it
# stays in the table for inspection but is no longer selected, so a poisoned
# row (e.g. its team/key was deleted while it sat in the queue) can't occupy
# the oldest-first batch forever and starve newer events.
MAX_RECONCILE_ATTEMPTS = 10


# SQLite has no `date_trunc`; strftime with a fixed minutes/seconds suffix
# truncates instead. `created_at` is stored (via `DateTimeUTC`) as a plain
# "YYYY-MM-DD HH:MM:SS.ffffff" string already normalized to UTC, so this needs
# no timezone conversion of its own.
_SQLITE_BUCKET_FORMAT: dict[str, str] = {
    "hour": "%Y-%m-%d %H:00:00",
    "day": "%Y-%m-%d 00:00:00",
}


def _bucket_key_expr(dialect_name: str, granularity: Literal["hour", "day"]) -> Any:
    """A GROUP-BY-able, dialect-portable UTC bucket key. Bucketing happens
    entirely in SQL (never a Python-side scan), and — critically for DST —
    entirely in UTC: Postgres's `date_trunc` truncates in the *session*
    timezone by default, so `created_at` (a `timestamptz`) is first converted
    with `AT TIME ZONE 'UTC'` to a naive UTC timestamp before truncation,
    regardless of what timezone the connection happens to be in."""
    if dialect_name == "postgresql":
        return func.date_trunc(granularity, func.timezone("UTC", UsageEventModel.created_at))
    return func.strftime(_SQLITE_BUCKET_FORMAT[granularity], UsageEventModel.created_at)


def _parse_bucket_key(value: Any, dialect_name: str) -> datetime:
    """The inverse of `_bucket_key_expr`: both branches already represent a
    UTC wall-clock instant, so this only needs to attach `tzinfo=UTC` (Postgres
    driver returns a naive `datetime` for the `AT TIME ZONE` result; SQLite
    returns the plain string produced by `strftime`)."""
    if dialect_name == "postgresql":
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


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
                requested_alias=event.requested_alias,
                resolved_model_id=event.resolved_model_id or event.model_id,
                canonical_model_name=event.canonical_model_name or event.model_name,
                callable_origin=event.callable_origin,
                source_team_id=event.source_team_id,
                operation=event.operation,
                prompt_tokens=event.prompt_tokens,
                completion_tokens=event.completion_tokens,
                cost=event.cost,
                created_at=event.created_at,
                cache_hit=event.cache_hit,
                cache_write_tokens=event.cache_write_tokens,
                cache_read_tokens=event.cache_read_tokens,
                image_count=event.image_count,
                request_id=event.request_id,
            )
        )
        await self._session.commit()

    async def list_events(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[UsageEvent]:
        rows = await self._session.scalars(
            select(UsageEventModel)
            .where(UsageEventModel.team_id == team_id)
            .order_by(UsageEventModel.created_at, UsageEventModel.id)
            .limit(limit)
            .offset(offset)
        )
        return [row.to_entity() for row in rows]

    async def aggregate(
        self,
        team_id: UUID,
        *,
        model_name: str | None = None,
        requested_alias: str | None = None,
        resolved_model_id: UUID | None = None,
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
                UsageEventModel.requested_alias,
                UsageEventModel.resolved_model_id,
                UsageEventModel.canonical_model_name,
                UsageEventModel.callable_origin,
                UsageEventModel.source_team_id,
                func.coalesce(func.sum(UsageEventModel.prompt_tokens), 0),
                func.coalesce(func.sum(UsageEventModel.completion_tokens), 0),
                func.coalesce(func.sum(UsageEventModel.cost), 0.0),
                func.count(),
            )
            .where(UsageEventModel.team_id == team_id)
            .group_by(
                UsageEventModel.model_id,
                UsageEventModel.model_name,
                UsageEventModel.requested_alias,
                UsageEventModel.resolved_model_id,
                UsageEventModel.canonical_model_name,
                UsageEventModel.callable_origin,
                UsageEventModel.source_team_id,
            )
            .order_by(
                UsageEventModel.requested_alias,
                UsageEventModel.model_name,
                UsageEventModel.model_id,
            )
            .limit(limit)
            .offset(offset)
        )
        if model_name is not None:
            # Backwards-compatible `model`: match what the caller requested OR
            # the canonical model that was actually billed.
            query = query.where(
                or_(
                    UsageEventModel.requested_alias == model_name,
                    UsageEventModel.canonical_model_name == model_name,
                    UsageEventModel.model_name == model_name,
                )
            )
        if requested_alias is not None:
            query = query.where(UsageEventModel.requested_alias == requested_alias)
        if resolved_model_id is not None:
            query = query.where(
                or_(
                    UsageEventModel.resolved_model_id == resolved_model_id,
                    UsageEventModel.model_id == resolved_model_id,
                )
            )
        if api_key_id is not None:
            query = query.where(UsageEventModel.api_key_id == api_key_id)

        rows = (await self._session.execute(query)).all()
        return [
            UsageAggregate(
                model_id=row[0],
                model_name=row[1],
                requested_alias=row[2],
                resolved_model_id=row[3] or row[0],
                canonical_model_name=row[4] or row[1],
                callable_origin=row[5],
                source_team_id=row[6],
                prompt_tokens=int(row[7]),
                completion_tokens=int(row[8]),
                cost=float(row[9]),
                calls=int(row[10]),
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
            .where(
                UsageEventModel.team_id == team_id,
                UsageEventModel.api_key_id.is_not(None),
            )
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
        return (total or 0.0) + (pending or 0.0)

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
                requested_alias=event.requested_alias,
                resolved_model_id=event.resolved_model_id or event.model_id,
                canonical_model_name=event.canonical_model_name or event.model_name,
                callable_origin=event.callable_origin,
                source_team_id=event.source_team_id,
                operation=event.operation,
                prompt_tokens=event.prompt_tokens,
                completion_tokens=event.completion_tokens,
                cost=event.cost,
                event_created_at=event.created_at,
                cache_hit=event.cache_hit,
                cache_write_tokens=event.cache_write_tokens,
                cache_read_tokens=event.cache_read_tokens,
                image_count=event.image_count,
                request_id=event.request_id,
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
                            requested_alias=row.requested_alias,
                            resolved_model_id=row.resolved_model_id or row.model_id,
                            canonical_model_name=row.canonical_model_name or row.model_name,
                            callable_origin=row.callable_origin,
                            source_team_id=row.source_team_id,
                            operation=row.operation,
                            prompt_tokens=row.prompt_tokens,
                            completion_tokens=row.completion_tokens,
                            cost=row.cost,
                            cache_hit=row.cache_hit,
                            cache_write_tokens=row.cache_write_tokens,
                            cache_read_tokens=row.cache_read_tokens,
                            image_count=row.image_count,
                            request_id=row.request_id,
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

    async def cache_savings(self, team_id: UUID) -> tuple[float, int, int, int]:
        return await self._cache_savings_aggregate(UsageEventModel.team_id == team_id)

    async def platform_cache_savings(self) -> tuple[float, int, int, int]:
        return await self._cache_savings_aggregate()

    async def _cache_savings_aggregate(self, *base: Any) -> tuple[float, int, int, int]:
        # One point-in-time query, mirroring the routing savings aggregate
        # (`router_repository.py`'s `_savings_aggregate`): a cache-hit event's
        # own stored token counts × the model's *current* unit price (joined by
        # model_id) is what the request would have cost — "priced" excludes
        # hits whose model has no cost configured (or was deleted).
        priced = and_(
            UsageEventModel.cache_hit.is_(True),
            ModelRecord.input_cost_per_token.is_not(None),
            ModelRecord.output_cost_per_token.is_not(None),
        )
        cost_expr = (
            ModelRecord.input_cost_per_token * UsageEventModel.prompt_tokens
            + ModelRecord.output_cost_per_token * UsageEventModel.completion_tokens
        )
        avoided, priced_hits, all_hits, total = (
            await self._session.execute(
                select(
                    func.coalesce(func.sum(case((priced, cost_expr), else_=0.0)), 0.0),
                    func.coalesce(func.sum(case((priced, 1), else_=0)), 0),
                    func.coalesce(
                        func.sum(case((UsageEventModel.cache_hit.is_(True), 1), else_=0)), 0
                    ),
                    func.count(),
                )
                .select_from(UsageEventModel)
                .outerjoin(ModelRecord, ModelRecord.id == UsageEventModel.model_id)
                .where(*base)
            )
        ).one()
        return (
            float(avoided or 0.0),
            int(priced_hits or 0),
            int((all_hits or 0) - (priced_hits or 0)),
            int(total or 0),
        )

    async def timeseries(
        self,
        team_id: UUID,
        *,
        start: datetime,
        end: datetime,
        granularity: Literal["hour", "day"],
        model_name: str | None = None,
        requested_alias: str | None = None,
        api_key_id: UUID | None = None,
        group_by: Literal["model"] | None = None,
    ) -> list[UsageBucket]:
        dialect_name = self._session.get_bind().dialect.name
        bucket_key = _bucket_key_expr(dialect_name, granularity)
        # Same label `UsageResponse.from_aggregate` uses for the per-model
        # table: requested alias, falling back to canonical model name.
        group_key_expr = func.coalesce(
            UsageEventModel.requested_alias, UsageEventModel.canonical_model_name
        )
        select_columns = [
            bucket_key.label("bucket_key"),
            func.count(),
            func.coalesce(func.sum(UsageEventModel.prompt_tokens), 0),
            func.coalesce(func.sum(UsageEventModel.completion_tokens), 0),
            func.coalesce(func.sum(UsageEventModel.cost), 0.0),
        ]
        group_by_columns = [bucket_key]
        if group_by == "model":
            select_columns.append(group_key_expr.label("group_key"))
            group_by_columns.append(group_key_expr)

        query = (
            select(*select_columns)
            .where(
                UsageEventModel.team_id == team_id,
                UsageEventModel.created_at >= start,
                UsageEventModel.created_at < end,
            )
            .group_by(*group_by_columns)
            .order_by(*group_by_columns)
        )
        if model_name is not None:
            query = query.where(
                or_(
                    UsageEventModel.requested_alias == model_name,
                    UsageEventModel.canonical_model_name == model_name,
                    UsageEventModel.model_name == model_name,
                )
            )
        if requested_alias is not None:
            query = query.where(UsageEventModel.requested_alias == requested_alias)
        if api_key_id is not None:
            query = query.where(UsageEventModel.api_key_id == api_key_id)

        rows = (await self._session.execute(query)).all()
        return [
            UsageBucket(
                bucket_start=_parse_bucket_key(row[0], dialect_name),
                request_count=int(row[1]),
                prompt_tokens=int(row[2]),
                completion_tokens=int(row[3]),
                cost=float(row[4]),
                group_key=(row[5] or "unknown") if group_by == "model" else None,
            )
            for row in rows
        ]

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
