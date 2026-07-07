"""Pure budget-window math: when does the current spend window start?"""

from __future__ import annotations

from datetime import UTC, datetime

from litestar_gateway.domain.budget import window_start
from litestar_gateway.domain.entities import BudgetWindow


def test_monthly_window_starts_at_first_of_month_utc() -> None:
    now = datetime(2026, 7, 15, 13, 45, 12, tzinfo=UTC)
    assert window_start(BudgetWindow.MONTHLY, now) == datetime(2026, 7, 1, tzinfo=UTC)


def test_daily_window_starts_at_midnight_utc() -> None:
    now = datetime(2026, 7, 15, 13, 45, 12, tzinfo=UTC)
    assert window_start(BudgetWindow.DAILY, now) == datetime(2026, 7, 15, tzinfo=UTC)


def test_monthly_window_at_exact_start_is_idempotent() -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    assert window_start(BudgetWindow.MONTHLY, start) == start


def test_daily_window_at_exact_start_is_idempotent() -> None:
    start = datetime(2026, 7, 15, tzinfo=UTC)
    assert window_start(BudgetWindow.DAILY, start) == start
