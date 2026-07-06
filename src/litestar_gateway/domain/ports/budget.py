"""Port — per-team budget persistence."""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from litestar_gateway.domain.entities import Budget


@runtime_checkable
class BudgetRepository(Protocol):
    """Persistence port for per-team spend caps (at most one budget per team)."""

    async def get(self, team_id: UUID) -> Budget | None: ...

    async def set(self, budget: Budget) -> Budget:
        """Create the team's budget, or replace it if one exists (upsert)."""
        ...

    async def remove(self, team_id: UUID) -> None: ...
