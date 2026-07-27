"""Pure budget-window math and threshold-alert evaluation (Plan 07 Phase 0)."""

from __future__ import annotations

from datetime import datetime

from litestar_gateway.domain.entities import BudgetWindow
from litestar_gateway.domain.exceptions import InvalidBudget


def window_start(window: BudgetWindow, now: datetime) -> datetime:
    """Start of the current spend window (calendar month/day, in `now`'s tz)."""
    if window is BudgetWindow.MONTHLY:
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def validate_thresholds(thresholds: list[int]) -> list[int]:
    """Boundary-validate a per-team alert threshold list (percentages of
    `Budget.limit_cost`, design doc §8): every value must be a plain `int` in
    `1..100`. Sorting and de-duplication are normalized here rather than
    rejected, so callers can pass thresholds in any order without an error —
    only genuinely out-of-range or wrong-typed values raise."""
    for value in thresholds:
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidBudget(f"threshold must be an int, got {value!r}")
        if not 1 <= value <= 100:
            raise InvalidBudget(f"threshold must be in 1..100, got {value}")
    return sorted(set(thresholds))


def crossed_thresholds(
    *, spend: float, limit_cost: float, thresholds: list[int], fired: set[int]
) -> list[int]:
    """Thresholds newly crossed by `spend` against `limit_cost`, excluding any
    already in `fired` (the persisted dedup set for the current
    `period_start`). Pure — no I/O, no persistence.

    A fresh/empty `fired` set (as passed by a caller after a period rollover,
    design doc §5) naturally re-fires every currently-crossed threshold: the
    helper has no memory of its own, so rollover correctness falls out of the
    caller scoping `fired` to `period_start` rather than out of any special
    case here. Returned in ascending order regardless of input order.
    """
    if limit_cost <= 0:
        return []
    pct = (spend / limit_cost) * 100
    return [t for t in sorted(thresholds) if t not in fired and pct >= t]
