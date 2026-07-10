"""Application service for organizations (platform-admin only)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from litestar_gateway.domain.entities import Organization, User
from litestar_gateway.domain.exceptions import (
    OrganizationNotEmpty,
    OrganizationNotFound,
    PermissionDenied,
)
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.domain.ports import OrganizationRepository, TeamRepository


def _now() -> datetime:
    return datetime.now(UTC)


def _require_platform_admin(actor: User) -> None:
    if not actor.is_admin:
        raise PermissionDenied("Platform admin privileges required")


class OrganizationService:
    def __init__(self, organizations: OrganizationRepository, teams: TeamRepository) -> None:
        self._orgs = organizations
        self._teams = teams

    async def create(self, actor: User, name: str) -> Organization:
        _require_platform_admin(actor)
        return await self._orgs.add(Organization(id=uuid4(), name=name, created_at=_now()))

    async def list(
        self, actor: User, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Organization]:
        _require_platform_admin(actor)
        return await self._orgs.list(limit=limit, offset=offset)

    async def rename(self, actor: User, organization_id: UUID, name: str) -> Organization:
        _require_platform_admin(actor)
        org = await self._orgs.update(organization_id, name)
        if org is None:
            raise OrganizationNotFound(str(organization_id))
        return org

    async def delete(self, actor: User, organization_id: UUID) -> Organization:
        """Delete an empty organization. Refuses (OrganizationNotEmpty) if it
        still has teams — no cascade — and returns the deleted entity so the
        caller can record what was removed."""
        _require_platform_admin(actor)
        org = await self._orgs.get(organization_id)
        if org is None:
            raise OrganizationNotFound(str(organization_id))
        teams = await self._teams.list_by_organization(organization_id, limit=1, offset=0)
        if teams:
            raise OrganizationNotEmpty(str(organization_id))
        await self._orgs.delete(organization_id)
        return org
