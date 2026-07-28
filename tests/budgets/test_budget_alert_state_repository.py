"""Persistence for the budget-alert dedup ledger + outbox (Plan 07 Phases 0-1).

Exercises `SQLAlchemyBudgetAlertStateRepository` directly against a SQLite
session, mirroring `tests/misc/test_usage_outbox.py`'s style for repository-
level tests. `record_fired`/`fired_thresholds` are wired into
`UsageMeter.settle_ok` as of Phase 1; `enqueue_alert`/`pending_alerts` are the
Phase 1 outbox side (delivery itself is Phase 2)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.domain.entities import BudgetWindow, PendingBudgetAlert
from litestar_gateway.domain.money import to_cost
from litestar_gateway.infrastructure.persistence.budget_alert_state_repository import (
    SQLAlchemyBudgetAlertStateRepository,
)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'alerts.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(base.UUIDAuditBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def test_fired_thresholds_starts_empty(session: AsyncSession) -> None:
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    team_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=UTC)

    assert await repo.fired_thresholds(team_id, BudgetWindow.MONTHLY, period_start) == set()


async def test_record_fired_is_reflected_in_fired_thresholds(session: AsyncSession) -> None:
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    team_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=UTC)

    state = await repo.record_fired(team_id, BudgetWindow.MONTHLY, period_start, 50)
    assert state is not None
    assert state.team_id == team_id
    assert state.window == BudgetWindow.MONTHLY
    assert state.period_start == period_start
    assert state.threshold == 50

    assert await repo.fired_thresholds(team_id, BudgetWindow.MONTHLY, period_start) == {50}


async def test_duplicate_dedup_key_is_rejected_not_duplicated(session: AsyncSession) -> None:
    """The unique constraint on (team_id, window, period_start, threshold)
    makes a second insert for the same key a no-op, not a second row — this
    is what makes concurrent settlements on different replicas safe."""
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    team_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=UTC)

    first = await repo.record_fired(team_id, BudgetWindow.MONTHLY, period_start, 80)
    assert first is not None

    second = await repo.record_fired(team_id, BudgetWindow.MONTHLY, period_start, 80)
    assert second is None

    assert await repo.fired_thresholds(team_id, BudgetWindow.MONTHLY, period_start) == {80}


async def test_different_periods_have_independent_fired_sets(session: AsyncSession) -> None:
    """A new period_start (window rollover) must not inherit the prior
    period's fired thresholds — the dedup key includes period_start."""
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    team_id = uuid4()
    july = datetime(2026, 7, 1, tzinfo=UTC)
    august = datetime(2026, 8, 1, tzinfo=UTC)

    await repo.record_fired(team_id, BudgetWindow.MONTHLY, july, 50)
    await repo.record_fired(team_id, BudgetWindow.MONTHLY, july, 80)

    assert await repo.fired_thresholds(team_id, BudgetWindow.MONTHLY, july) == {50, 80}
    assert await repo.fired_thresholds(team_id, BudgetWindow.MONTHLY, august) == set()


async def test_daily_and_monthly_windows_are_independent(session: AsyncSession) -> None:
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    team_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=UTC)

    await repo.record_fired(team_id, BudgetWindow.MONTHLY, period_start, 50)

    assert await repo.fired_thresholds(team_id, BudgetWindow.MONTHLY, period_start) == {50}
    assert await repo.fired_thresholds(team_id, BudgetWindow.DAILY, period_start) == set()


async def test_enqueue_alert_is_readable_via_pending_alerts(session: AsyncSession) -> None:
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    team_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=UTC)
    alert = PendingBudgetAlert(
        id=uuid4(),
        team_id=team_id,
        window=BudgetWindow.MONTHLY,
        period_start=period_start,
        threshold=80,
        spend=to_cost("85.0"),
        limit_cost=to_cost("100.0"),
        created_at=datetime.now(UTC),
    )

    await repo.enqueue_alert(alert)

    pending = await repo.pending_alerts()
    assert len(pending) == 1
    row = pending[0]
    assert row.id == alert.id
    assert row.team_id == team_id
    assert row.window == BudgetWindow.MONTHLY
    assert row.period_start == period_start
    assert row.threshold == 80
    assert row.spend == 85.0
    assert row.limit_cost == 100.0


async def test_pending_alerts_starts_empty(session: AsyncSession) -> None:
    repo = SQLAlchemyBudgetAlertStateRepository(session)

    assert await repo.pending_alerts() == []


async def test_pending_alerts_respects_limit_oldest_first(session: AsyncSession) -> None:
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    team_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=UTC)
    for threshold in (50, 80, 100):
        await repo.enqueue_alert(
            PendingBudgetAlert(
                id=uuid4(),
                team_id=team_id,
                window=BudgetWindow.MONTHLY,
                period_start=period_start,
                threshold=threshold,
                spend=to_cost(str(threshold)),
                limit_cost=to_cost("100.0"),
                created_at=datetime.now(UTC),
            )
        )

    pending = await repo.pending_alerts(limit=2)

    assert [row.threshold for row in pending] == [50, 80]


# ---------------------------------------------------------------------------
# ISSUE-026: the dedup row and the outbox row are one durable fact.
# ---------------------------------------------------------------------------


def _pending(team_id, threshold: int = 80, period_start=None) -> PendingBudgetAlert:
    return PendingBudgetAlert(
        id=uuid4(),
        team_id=team_id,
        window=BudgetWindow.MONTHLY,
        period_start=period_start or datetime(2026, 7, 1, tzinfo=UTC),
        threshold=threshold,
        spend=to_cost("85.0"),
        limit_cost=to_cost("100.0"),
        created_at=datetime.now(UTC),
    )


async def test_record_fired_and_enqueue_writes_both_rows(session: AsyncSession) -> None:
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    team_id = uuid4()
    alert = _pending(team_id)

    state = await repo.record_fired_and_enqueue(alert)

    assert state is not None
    assert await repo.fired_thresholds(team_id, BudgetWindow.MONTHLY, alert.period_start) == {80}
    assert [a.id for a in await repo.pending_alerts()] == [alert.id]


async def test_a_duplicate_dedup_key_queues_nothing(session: AsyncSession) -> None:
    # The loser of the race must not leave a second outbox row behind: its
    # rollback has to discard both inserts, not just the conflicting one.
    repo = SQLAlchemyBudgetAlertStateRepository(session)
    team_id = uuid4()
    first = _pending(team_id)
    assert await repo.record_fired_and_enqueue(first) is not None

    second = _pending(team_id)  # same dedup key, different row id
    assert await repo.record_fired_and_enqueue(second) is None

    assert [a.id for a in await repo.pending_alerts()] == [first.id]
