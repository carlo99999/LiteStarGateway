"""Port — per-team budget persistence."""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from litestar_gateway.domain.entities import Budget


@runtime_checkable
class BudgetRepository(Protocol):
    """Persistence port for per-team spend caps (at most one budget per team)."""

    async def get(self, team_id: UUID) -> Budget | None: ...

    async def set(
        self,
        budget: Budget,
        *,
        alert_webhook_secret: str | None = None,
        clear_alert_webhook_secret: bool = False,
    ) -> Budget:
        """Create the team's budget, or replace it if one exists (upsert).

        `alert_webhook_secret=None` keeps the stored one: it is never readable,
        so an operator editing a threshold cannot resubmit it, and treating
        omission as "clear" would silently downgrade a signed endpoint to the
        platform-wide key. Removing it is therefore its own flag rather than a
        value — with only "keep" and "replace" the platform-wide secret was
        unreachable once a team secret had been set."""
        ...

    async def alert_webhook_secret(self, team_id: UUID) -> str | None:
        """The team's own webhook HMAC secret, decrypted — for the dispatcher
        that signs with it, and nothing else. `None` means the platform-wide
        secret applies."""
        ...

    async def remove(self, team_id: UUID) -> None: ...
