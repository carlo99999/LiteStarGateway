"""Durable-billing outbox: dead-lettered usage events + background reconciliation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.domain.entities import UsageEvent
from litestar_gateway.infrastructure.persistence.usage_repository import SQLAlchemyUsageRepository


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
