"""Port — per-key spend caps."""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from litestar_gateway.domain.entities import ApiKeyBudget


@runtime_checkable
class ApiKeyBudgetRepository(Protocol):
    """At most one cap per API key, replaced on write.

    Read on the admission path for every request that carries a key, so `get`
    must stay a single indexed lookup.
    """

    async def get(self, api_key_id: UUID) -> ApiKeyBudget | None: ...

    async def list_for_team(self, team_id: UUID) -> list[ApiKeyBudget]: ...

    async def set(self, budget: ApiKeyBudget) -> ApiKeyBudget:
        """Create the key's cap, or replace it if one exists (upsert)."""
        ...

    async def stage_set(self, budget: ApiKeyBudget) -> ApiKeyBudget:
        """`set` without committing, for a caller that owns the transaction."""
        ...

    async def remove(self, api_key_id: UUID) -> bool: ...
