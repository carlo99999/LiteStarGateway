"""Port — dedup ledger + delivery outbox for budget-threshold alerts.

Phase 0 shipped the dedup ledger (`fired_thresholds`/`record_fired`). Plan 07
Phase 1 adds the outbox side (`enqueue_alert`/`pending_alerts`): once a
threshold is newly recorded as fired, a `PendingBudgetAlert` row is queued for
later delivery, mirroring how `UsageRepository` combines the usage ledger and
its `pending_usage_event` outbox on one port (`domain/ports/usage.py:47-55`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from litestar_gateway.domain.entities import BudgetAlertState, BudgetWindow, PendingBudgetAlert
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.domain.ports.notification_channel import NotificationChannel


@runtime_checkable
class BudgetAlertStateRepository(Protocol):
    """Persistence port for the `(team_id, window, period_start, threshold)`
    dedup ledger plus the `pending_budget_alert` delivery outbox. Phase 1 wires
    this into `UsageMeter.settle_ok`: a newly-crossed threshold is recorded via
    `record_fired`, and only if that returns a new row is `enqueue_alert`
    called — never the reverse, so a threshold can never be queued for
    delivery without also being marked fired."""

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

    async def enqueue_alert(self, alert: PendingBudgetAlert) -> None:
        """Durable outbox row for a newly-fired threshold, so delivery survives
        restarts and stays off the settlement hot path (Plan 07 Phase 1, design
        doc §4). Callers must only enqueue after `record_fired` returns a new
        row — never for a dedup key that was already fired."""
        ...

    async def pending_alerts(self, *, limit: int = DEFAULT_PAGE_SIZE) -> list[PendingBudgetAlert]:
        """Queued alerts oldest-first, for tests and `dispatch_pending` to
        build on."""
        ...

    async def dispatch_pending(
        self, channels: Sequence[NotificationChannel], *, limit: int = DEFAULT_PAGE_SIZE
    ) -> int:
        """Drain up to `limit` pending alerts oldest-first (Plan 07 Phase 2):
        dispatch each through every `channel`, delete the row on success, or
        bump `attempts`/`last_error` and leave it queued for retry on
        failure — the same poison-quarantine convention as
        `UsageRepository.reconcile_pending`. Returns how many were
        delivered. An empty `channels` sequence is a no-op."""
        ...
