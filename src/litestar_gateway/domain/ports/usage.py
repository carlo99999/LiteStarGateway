"""Port — usage event persistence and aggregation."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from litestar_gateway.domain.entities import ApiKeySpend, UsageAggregate, UsageEvent
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE


@runtime_checkable
class UsageRepository(Protocol):
    """Persistence port for recorded usage events + aggregation."""

    async def record(self, event: UsageEvent) -> None: ...

    async def aggregate(
        self,
        team_id: UUID,
        *,
        model_name: str | None = None,
        requested_alias: str | None = None,
        resolved_model_id: UUID | None = None,
        api_key_id: UUID | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[UsageAggregate]:
        """Usage grouped by requested alias and resolved model identity.

        ``model_name`` is the compatible broad filter (alias OR canonical
        model); the other filters are explicit and exact.
        """
        ...

    async def spend_by_api_key(self, team_id: UUID) -> list[ApiKeySpend]:
        """Token/cost totals grouped by API key for the team (includes keys that
        are now revoked, as long as they have recorded usage)."""
        ...

    async def spend_since(self, team_id: UUID, since: datetime) -> float:
        """Total cost recorded for the team from `since` onwards. Read on the
        hot path by the budget gate — must stay a cheap indexed aggregate."""
        ...

    async def enqueue_pending(self, event: UsageEvent) -> None:
        """Durable dead-letter for a usage event whose ledger write failed, so a
        background reconciler can retry it instead of the event being lost."""
        ...

    async def reconcile_pending(self, *, limit: int = DEFAULT_PAGE_SIZE) -> int:
        """Move up to `limit` dead-lettered usage events into the ledger (idempotent
        by event id), removing settled ones. Returns how many were settled."""
        ...
