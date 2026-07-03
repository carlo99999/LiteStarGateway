"""Durable-billing outbox: dead-lettered usage events + background reconciliation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.domain.entities import UsageEvent
from litestar_gateway.infrastructure.persistence.orm import PendingUsageEventModel
from litestar_gateway.infrastructure.persistence.usage_repository import (
    MAX_RECONCILE_ATTEMPTS,
    SQLAlchemyUsageRepository,
)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'usage.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(base.UUIDAuditBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _event(created_at: datetime | None = None) -> UsageEvent:
    return UsageEvent(
        id=uuid4(),
        team_id=uuid4(),
        api_key_id=uuid4(),
        model_id=uuid4(),
        model_name="m",
        operation="chat.completions",
        prompt_tokens=5,
        completion_tokens=7,
        cost=0.12,
        created_at=created_at or datetime.now(UTC),
    )


async def test_enqueue_then_reconcile_lands_in_ledger(session: AsyncSession) -> None:
    repo = SQLAlchemyUsageRepository(session)
    event = _event()

    await repo.enqueue_pending(event)
    # Not in the ledger yet — still dead-lettered.
    assert await repo.aggregate(event.team_id) == []

    settled = await repo.reconcile_pending()
    assert settled == 1

    rows = await repo.aggregate(event.team_id)
    assert len(rows) == 1
    assert rows[0].prompt_tokens == 5
    assert rows[0].completion_tokens == 7
    assert rows[0].calls == 1

    # Idempotent + drained: a second reconcile settles nothing.
    assert await repo.reconcile_pending() == 0


async def test_reconcile_is_idempotent_if_event_already_recorded(session: AsyncSession) -> None:
    repo = SQLAlchemyUsageRepository(session)
    event = _event()
    # The event was actually recorded to the ledger, but a duplicate also got
    # dead-lettered. Reconciling must not double-count it.
    await repo.record(event)
    await repo.enqueue_pending(event)

    settled = await repo.reconcile_pending()
    assert settled == 1
    rows = await repo.aggregate(event.team_id)
    assert len(rows) == 1
    assert rows[0].calls == 1  # not 2


async def test_spend_since_counts_dead_lettered_cost(session: AsyncSession) -> None:
    # Dead-lettered spend is real cost (already billed upstream): the budget
    # gate must see it while it waits in the outbox, not only after the
    # reconciler drains it — otherwise a write degradation window doubles as
    # a budget-cap bypass window.
    repo = SQLAlchemyUsageRepository(session)
    event = _event()
    await repo.enqueue_pending(event)

    assert await repo.spend_since(event.team_id, event.created_at - timedelta(minutes=1)) == 0.12
    # Window filtering applies to the outbox too (by the event's own time).
    assert await repo.spend_since(event.team_id, event.created_at + timedelta(minutes=1)) == 0.0

    # After the drain the event lives in the ledger instead — same total,
    # never double-counted.
    assert await repo.reconcile_pending() == 1
    assert await repo.spend_since(event.team_id, event.created_at - timedelta(minutes=1)) == 0.12


def _poison_get(session: AsyncSession, poison_event_id) -> None:
    """Make the ledger lookup fail permanently for one event — the outbox
    equivalent of a row whose insert violates an FK (team/key deleted while
    the event sat in the queue)."""
    real_get = session.get

    async def failing_get(entity, pk, *args, **kwargs):
        if pk == poison_event_id:
            raise RuntimeError("permanently failing row")
        return await real_get(entity, pk, *args, **kwargs)

    session.get = failing_get  # type: ignore[method-assign]


async def test_poisoned_row_cannot_starve_newer_events(session: AsyncSession) -> None:
    # A permanently-failing row at the head of the oldest-first batch must not
    # monopolize reconciliation forever: after MAX_RECONCILE_ATTEMPTS it is
    # quarantined (skipped) and newer events settle again.
    repo = SQLAlchemyUsageRepository(session)
    poison = _event()
    fresh = _event()
    await repo.enqueue_pending(poison)
    await repo.enqueue_pending(fresh)
    _poison_get(session, poison.id)

    # limit=1 → the poison row is the whole batch until it runs out of attempts.
    for _ in range(MAX_RECONCILE_ATTEMPTS):
        assert await repo.reconcile_pending(limit=1) == 0

    # Quarantined: the next cycle selects past it and settles the fresh event.
    assert await repo.reconcile_pending(limit=1) == 1
    assert len(await repo.aggregate(fresh.team_id)) == 1

    # The poison row is kept for inspection, with its failure bookkeeping.
    remaining = (await session.scalars(select(PendingUsageEventModel))).all()
    assert [r.event_id for r in remaining] == [poison.id]
    assert remaining[0].attempts == MAX_RECONCILE_ATTEMPTS
    assert "permanently failing row" in (remaining[0].last_error or "")


async def test_transient_failure_is_retried_and_settles(session: AsyncSession) -> None:
    # A row that fails once (transient DB blip) is not lost: its attempt is
    # counted, and the next cycle settles it normally.
    repo = SQLAlchemyUsageRepository(session)
    event = _event()
    await repo.enqueue_pending(event)

    real_get = session.get
    _poison_get(session, event.id)
    assert await repo.reconcile_pending() == 0

    session.get = real_get  # type: ignore[method-assign]
    assert await repo.reconcile_pending() == 1
    assert len(await repo.aggregate(event.team_id)) == 1
    assert (await session.scalars(select(PendingUsageEventModel))).all() == []


async def test_reconcile_preserves_original_event_time(session: AsyncSession) -> None:
    # An event dead-lettered in one budget window and drained in the next must
    # count against the window it happened in, not the reconcile time — else
    # July's spend lands in August's budget and monthly aggregates.
    repo = SQLAlchemyUsageRepository(session)
    happened_at = datetime.now(UTC) - timedelta(days=30)
    event = _event(created_at=happened_at)

    await repo.enqueue_pending(event)
    assert await repo.reconcile_pending() == 1

    # Same query the budget gate runs: a window containing the original time
    # sees the cost; a window that starts after it must not.
    assert await repo.spend_since(event.team_id, happened_at - timedelta(days=1)) == 0.12
    assert await repo.spend_since(event.team_id, happened_at + timedelta(days=1)) == 0.0


async def test_record_preserves_event_time(session: AsyncSession) -> None:
    # The ledger insert must stamp the event's own timestamp too, so direct
    # writes and reconciled writes agree on when the spend happened.
    repo = SQLAlchemyUsageRepository(session)
    happened_at = datetime.now(UTC) - timedelta(days=30)
    event = _event(created_at=happened_at)

    await repo.record(event)

    assert await repo.spend_since(event.team_id, happened_at - timedelta(days=1)) == 0.12
    assert await repo.spend_since(event.team_id, happened_at + timedelta(days=1)) == 0.0
