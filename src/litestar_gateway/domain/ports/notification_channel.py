"""Port — pluggable delivery channel for budget-threshold alerts (Plan 07
Phase 2, design doc §4). Kept abstract on purpose: `send` takes only the
alert itself, so a channel resolves its own delivery target (a webhook URL,
an SMTP recipient list, ...) from its own construction/config rather than
from a parameter here — nothing transport-specific (URLs, SMTP credentials)
crosses into `domain`/`application`. This lets Phase 3's email adapter
implement the exact same Protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from litestar_gateway.domain.entities import PendingBudgetAlert


@runtime_checkable
class NotificationChannel(Protocol):
    async def send(self, alert: PendingBudgetAlert) -> None:
        """Deliver one alert. Raise on any failure — the outbox worker
        (`infrastructure/budget_alert_reconciler.py`) treats an exception as
        a delivery failure: it bumps `attempts`/`last_error` on the
        `pending_budget_alert` row and leaves it queued for retry, the same
        poison-quarantine convention as the usage-billing outbox. Must never
        be called from the request path — only the background worker calls
        this."""
        ...
