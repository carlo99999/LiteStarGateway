"""Port — service principal persistence."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from litestar_gateway.domain.entities import ServicePrincipal
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE


class ServicePrincipalRepository(Protocol):
    """Persistence port for team service principals."""

    async def add(self, sp: ServicePrincipal) -> ServicePrincipal: ...

    async def get(self, sp_id: UUID) -> ServicePrincipal | None: ...

    async def get_for_update(self, sp_id: UUID) -> ServicePrincipal | None:
        """Load and lock a service principal for a credential lifecycle change."""
        ...

    async def list_by_team(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[ServicePrincipal]: ...

    async def set_enabled(self, sp_id: UUID, enabled: bool) -> ServicePrincipal: ...

    async def remove(self, sp_id: UUID) -> None:
        """Stage deletion; the application service owns the commit."""
        ...
