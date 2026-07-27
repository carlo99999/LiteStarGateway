"""Port — usage event persistence and aggregation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from litestar_gateway.domain.entities import ApiKeySpend, UsageAggregate, UsageBucket, UsageEvent
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE


@runtime_checkable
class UsageRepository(Protocol):
    """Persistence port for recorded usage events + aggregation."""

    async def record(self, event: UsageEvent) -> None: ...

    async def list_events(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[UsageEvent]:
        """Raw (non-aggregated) usage events for the team, oldest first — the
        export-before-delete action's usage dump (Plan 13 Phase 5)."""
        ...

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

    async def cache_savings(self, team_id: UUID) -> tuple[float, int, int, int]:
        """Response-cache observability for one team (Plan 04 Phase 3):
        ``(avoided_cost, priced_hits, hits_without_price, total_events)``.

        ``avoided_cost`` sums, over cache-hit events whose model still has a
        priced unit cost, the stored token counts × the model's *current* unit
        price. ``priced_hits`` + ``hits_without_price`` is the total hit count;
        ``total_events`` is every usage event for the team (hit rate
        denominator)."""
        ...

    async def platform_cache_savings(self) -> tuple[float, int, int, int]:
        """Same as `cache_savings`, across every team (platform-admin dashboard)."""
        ...

    async def timeseries(
        self,
        team_id: UUID,
        *,
        start: datetime,
        end: datetime,
        granularity: Literal["hour", "day"],
        model_name: str | None = None,
        requested_alias: str | None = None,
        api_key_id: UUID | None = None,
        group_by: Literal["model"] | None = None,
    ) -> list[UsageBucket]:
        """Bucketed usage totals for the team over ``[start, end)`` (Plan 10
        Phase 1), aggregated in SQL — never a Python-side full-table scan.

        Bucket boundaries are UTC-aligned regardless of server timezone, so a
        range spanning a DST transition still produces evenly-spaced buckets.
        Filters mirror `aggregate`'s semantics: ``model_name`` is the broad
        alias-or-canonical match, ``requested_alias``/``api_key_id`` are exact.
        A bucket with no matching events is omitted, not zero-filled.

        ``group_by="model"`` (Plan 10 Phase 2) additionally groups each bucket
        by requested-alias-or-canonical-model-name, emitting one `UsageBucket`
        per ``(bucket_start, group_key)`` pair instead of one per
        ``bucket_start`` — the console's per-model stacked chart needs exactly
        this shape from a single call rather than one request per model."""
        ...
