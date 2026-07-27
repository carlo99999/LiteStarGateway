"""Port — dedup ledger + delivery outbox for budget-threshold alerts.

Phase 0 shipped the dedup ledger (`fired_thresholds`/`record_fired`). Plan 07
Phase 1 adds the outbox side (`enqueue_alert`/`pending_alerts`): once a
threshold is newly recorded as fired, a `PendingBudgetAlert` row is queued for
later delivery, mirroring how `UsageRepository` combines the usage ledger and
its `pending_usage_event` outbox on one port (`domain/ports/usage.py:47-55`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from litestar_gateway.domain.entities import BudgetAlertState, BudgetWindow, PendingBudgetAlert
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.domain.ports.notification_channel import ChannelResolver


@runtime_checkable
class BudgetAlertStateRepository(Protocol):
    """Persistence port for the `(team_id, window, period_start, threshold)`
    dedup ledger plus the `pending_budget_alert` delivery outbox. Phase 1 wires
    this into `UsageMeter.settle_ok`: a newly-crossed threshold is recorded via
    `record_fired_and_enqueue`, which writes the dedup row and the outbox row
    in a single transaction: "fired" and "queued" are one durable fact, so
    neither a crash between two commits nor a concurrent settlement can leave a
    threshold marked fired with nothing to deliver (ISSUE-026)."""

    async def fired_thresholds(
        self, team_id: UUID, window: BudgetWindow, period_start: datetime
    ) -> set[int]:
        """Thresholds already recorded as fired for this team/window/period."""
        ...

    async def record_fired_and_enqueue(self, alert: PendingBudgetAlert) -> BudgetAlertState | None:
        """Record a newly-fired threshold in the dedup ledger AND queue its
        delivery, atomically. Returns the created ledger row, or `None` if a
        concurrent settlement already recorded the same dedup key — in which
        case nothing is queued either (the unique constraint on
        `(team_id, window, period_start, threshold)` makes the loser's insert a
        no-op rather than an error, the same PK-conflict strategy as the usage
        reconciler).

        Deliberately the only write on this port: exposing the two inserts
        separately is what allowed a fired-but-never-queued threshold to be
        skipped forever by later evaluations (ISSUE-026)."""
        ...

    async def pending_alerts(self, *, limit: int = DEFAULT_PAGE_SIZE) -> list[PendingBudgetAlert]:
        """Queued alerts oldest-first, for tests and `dispatch_pending` to
        build on."""
        ...

    async def recent_fired(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE
    ) -> list[BudgetAlertState]:
        """A team's most-recently fired alerts, newest-first — the read-model
        behind the console's recent-alerts list (Plan 07 Phase 3, design §8)."""
        ...

    async def dispatch_pending(
        self, resolve_channels: ChannelResolver, *, limit: int = DEFAULT_PAGE_SIZE
    ) -> int:
        """Drain up to `limit` pending alerts oldest-first: for each row,
        resolve the channel(s) for its owning team via `resolve_channels`
        (Plan 07 Phase 3 — delivery targets are per-team data, not a fixed
        list), dispatch through every resolved channel, delete on success, or
        bump `attempts`/`last_error` and leave it queued for retry on failure —
        the same poison-quarantine convention as
        `UsageRepository.reconcile_pending`. A row that resolves to no channels
        is skipped untouched. Returns how many were delivered."""
        ...
