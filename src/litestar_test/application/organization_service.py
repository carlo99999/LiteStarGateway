"""Application service for organizations (platform-admin only)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from litestar_test.domain.entities import Organization, User
from litestar_test.domain.exceptions import PermissionDenied
from litestar_test.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_test.domain.ports import OrganizationRepository


def _now() -> datetime:
    return datetime.now(UTC)


def _require_platform_admin(actor: User) -> None:
    if not actor.is_admin:
        raise PermissionDenied("Platform admin privileges required")


class OrganizationService:
    def __init__(self, organizations: OrganizationRepository) -> None:
        self._orgs = organizations

    async def create(self, actor: User, name: str) -> Organization:
        _require_platform_admin(actor)
        return await self._orgs.add(Organization(id=uuid4(), name=name, created_at=_now()))

    async def list(
        self, actor: User, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Organization]:
        _require_platform_admin(actor)
        return await self._orgs.list(limit=limit, offset=offset)
