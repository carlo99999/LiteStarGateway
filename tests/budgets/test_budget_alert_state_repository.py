"""Persistence for the budget-alert dedup ledger (Plan 07 Phase 0).

Exercises `SQLAlchemyBudgetAlertStateRepository` directly against a SQLite
session, mirroring `tests/misc/test_usage_outbox.py`'s style for repository-
level tests. Not wired into any request path yet (Phase 1)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.domain.entities import BudgetWindow
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
