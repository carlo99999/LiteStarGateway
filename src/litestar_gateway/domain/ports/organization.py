"""Port — organization persistence."""

from __future__ import annotations

from collections.abc import Sequence
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

    async def update(
        self, organization_id: UUID, name: str, description: str | None, tags: Sequence[str]
    ) -> Organization | None:
        """Replace the organization's editable metadata (name, description, tags);
        return the updated entity, or None if no organization has that id."""
        ...

    async def delete(self, organization_id: UUID) -> bool:
        """Delete the organization; return True if a row was removed, False if no
        organization has that id."""
        ...
