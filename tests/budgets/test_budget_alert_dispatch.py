"""Plan 07 Phase 2 — outbox drain-and-dispatch.

`SQLAlchemyBudgetAlertStateRepository.dispatch_pending` is the Phase 2
drain-and-dispatch method Phase 1 explicitly deferred (its `pending_alerts`
docstring). It mirrors `SQLAlchemyUsageRepository.reconcile_pending`'s shape:
oldest-first batch, delete on success, bump attempts/last_error and leave
queued on failure, quarantine past MAX_DISPATCH_ATTEMPTS."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.domain.entities import BudgetWindow, PendingBudgetAlert
from litestar_gateway.infrastructure.persistence.budget_alert_state_repository import (
    MAX_DISPATCH_ATTEMPTS,
    SQLAlchemyBudgetAlertStateRepository,
)
from litestar_gateway.infrastructure.persistence.orm import PendingBudgetAlertModel


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'dispatch.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(base.UUIDAuditBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _alert(**overrides) -> PendingBudgetAlert:
    defaults = dict(
        id=uuid4(),
        team_id=uuid4(),
        window=BudgetWindow.MONTHLY,
        period_start=datetime(2026, 7, 1, tzinfo=UTC),
        threshold=80,
        spend=85.0,
        limit_cost=100.0,
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return PendingBudgetAlert(**defaults)


class _RecordingChannel:
    def __init__(self) -> None:
        self.sent: list[PendingBudgetAlert] = []

    async def send(self, alert: PendingBudgetAlert) -> None:
        self.sent.append(alert)


class _FailingChannel:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    async def send(self, alert: PendingBudgetAlert) -> None:
        self.calls += 1
        raise self._error


async def test_dispatch_pending_delivers_and_deletes_on_success(session: AsyncSession) -> None:
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    alert = _alert()
    await repo.enqueue_alert(alert)
    channel = _RecordingChannel()

    delivered = await repo.dispatch_pending([channel])

    assert delivered == 1
    assert [a.id for a in channel.sent] == [alert.id]
    assert await repo.pending_alerts() == []


async def test_failing_channel_leaves_row_queued_and_records_error(
    session: AsyncSession,
) -> None:
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    alert = _alert()
    await repo.enqueue_alert(alert)
    channel = _FailingChannel(RuntimeError("webhook unreachable"))

    delivered = await repo.dispatch_pending([channel])

    assert delivered == 0
    row = (await session.execute(select(PendingBudgetAlertModel))).scalar_one()
    assert row.attempts == 1
    assert "webhook unreachable" in (row.last_error or "")


async def test_no_channels_configured_is_a_no_op(session: AsyncSession) -> None:
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    await repo.enqueue_alert(_alert())

    delivered = await repo.dispatch_pending([])

    assert delivered == 0
    assert len(await repo.pending_alerts()) == 1  # untouched, not marked failed


async def test_one_bad_row_does_not_block_the_rest_of_the_batch(
    session: AsyncSession,
) -> None:
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    bad = _alert(threshold=50)
    good = _alert(threshold=80)
    await repo.enqueue_alert(bad)
    await repo.enqueue_alert(good)

    class _SelectiveChannel:
        async def send(self, alert: PendingBudgetAlert) -> None:
            if alert.threshold == 50:
                raise RuntimeError("boom")

    delivered = await repo.dispatch_pending([_SelectiveChannel()])

    assert delivered == 1
    remaining = await repo.pending_alerts()
    assert [row.threshold for row in remaining] == [50]


async def test_row_is_quarantined_after_max_attempts(session: AsyncSession) -> None:
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    alert = _alert()
    await repo.enqueue_alert(alert)
    channel = _FailingChannel(RuntimeError("still down"))

    for _ in range(MAX_DISPATCH_ATTEMPTS):
        await repo.dispatch_pending([channel])

    # Quarantined: still present, but no longer selected for dispatch.
    row = (await session.execute(select(PendingBudgetAlertModel))).scalar_one()
    assert row.attempts == MAX_DISPATCH_ATTEMPTS
    delivered = await repo.dispatch_pending([channel])
    assert delivered == 0
    assert channel.calls == MAX_DISPATCH_ATTEMPTS  # not retried once quarantined
