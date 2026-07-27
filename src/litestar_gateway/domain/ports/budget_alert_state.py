"""Port — dedup ledger for fired budget-threshold alerts (Plan 07 Phase 0)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from litestar_gateway.domain.entities import BudgetAlertState, BudgetWindow


@runtime_checkable
class BudgetAlertStateRepository(Protocol):
    """Persistence port for the `(team_id, window, period_start, threshold)`
    dedup ledger. Deliberately minimal: just enough for the Phase 0 pure
    helper (`domain.budget.crossed_thresholds`) to be fed an already-fired set
    and for a newly-crossed threshold to be recorded. Phase 1 wires this into
    `UsageMeter.settle_ok`, in the same transaction as the outbox enqueue —
    not used by any request path yet."""

    async def fired_thresholds(
        self, team_id: UUID, window: BudgetWindow, period_start: datetime
    ) -> set[int]:
        """Thresholds already recorded as fired for this team/window/period."""
        ...

    async def record_fired(
        self,
        team_id: UUID,
        window: BudgetWindow,
        period_start: datetime,
        threshold: int,
    ) -> BudgetAlertState | None:
        """Persist a newly-fired threshold. Returns the created row, or `None`
        if a concurrent settlement already recorded the same dedup key (the
        unique constraint on `(team_id, window, period_start, threshold)`
        makes the loser's insert a no-op rather than an error — the same
        PK-conflict strategy as the usage reconciler)."""
        ...
