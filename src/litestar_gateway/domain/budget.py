"""Pure budget-window math."""

from __future__ import annotations

from datetime import datetime

from litestar_gateway.domain.entities import BudgetWindow


def window_start(window: BudgetWindow, now: datetime) -> datetime:
    """Start of the current spend window (calendar month/day, in `now`'s tz)."""
    if window is BudgetWindow.MONTHLY:
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)
