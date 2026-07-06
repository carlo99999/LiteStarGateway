"""Port — organization persistence."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from litestar_gateway.domain.entities import Organization
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE


class OrganizationRepository(Protocol):
    """Persistence port for organizations."""

    async def add(self, organization: Organization) -> Organization: ...

    async def get(self, organization_id: UUID) -> Organization | None: ...

    async def list(
        self, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Organization]: ...
