"""Application service for team service principals + their keys.

Administration is JWT-only (a human team admin, enforced by the controller via
`TeamService.ensure_team_permission`) — a key, even a management one, can never
create service principals or mint keys, so a leaked key cannot self-replicate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from litestar_gateway.application.service import APIKeyService
from litestar_gateway.domain.entities import IssuedKey, KeyScope, ServicePrincipal
from litestar_gateway.domain.exceptions import (
    InvalidServicePrincipal,
    ServicePrincipalNotFound,
)
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.domain.ports import ServicePrincipalRepository


def _now() -> datetime:
    return datetime.now(UTC)


class ServicePrincipalService:
    def __init__(
        self,
        service_principals: ServicePrincipalRepository,
        api_keys: APIKeyService,
    ) -> None:
        self._sps = service_principals
        self._keys = api_keys

    _MAX_NAME = 200

    async def create(self, team_id: UUID, name: str) -> ServicePrincipal:
        name = name.strip()
        if not name or len(name) > self._MAX_NAME:
            raise InvalidServicePrincipal(f"name must be 1..{self._MAX_NAME} characters")
        return await self._sps.add(
            ServicePrincipal(
                id=uuid4(), team_id=team_id, name=name, enabled=True, created_at=_now()
            )
        )

    async def list(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[ServicePrincipal]:
        return await self._sps.list_by_team(team_id, limit=limit, offset=offset)

    async def get(self, team_id: UUID, sp_id: UUID) -> ServicePrincipal | None:
        """Fetch an SP scoped to the team, or None (used by the principal loader,
        which must not raise on a dangling reference)."""
        sp = await self._sps.get(sp_id)
        return sp if sp is not None and sp.team_id == team_id else None

    async def _get_in_team(self, team_id: UUID, sp_id: UUID) -> ServicePrincipal:
        sp = await self._sps.get(sp_id)
        if sp is None or sp.team_id != team_id:
            raise ServicePrincipalNotFound(str(sp_id))
        return sp

    async def set_enabled(self, team_id: UUID, sp_id: UUID, enabled: bool) -> ServicePrincipal:
        await self._get_in_team(team_id, sp_id)
        return await self._sps.set_enabled(sp_id, enabled)

    async def delete(self, team_id: UUID, sp_id: UUID) -> None:
        await self._get_in_team(team_id, sp_id)
        # Revoke the SP's keys first: the identity is going away, its
        # credentials must not outlive it.
        await self._keys.revoke_for_service_principal(sp_id, _now())
        await self._sps.remove(sp_id)

    async def issue_key(
        self,
        team_id: UUID,
        sp_id: UUID,
        created_by: UUID,
        name: str | None,
        scope: KeyScope,
        rate_limit_rpm: int | None = None,
    ) -> IssuedKey:
        await self._get_in_team(team_id, sp_id)
        return await self._keys.issue(
            team_id=team_id,
            created_by=created_by,
            name=name,
            scope=scope,
            service_principal_id=sp_id,
            rate_limit_rpm=rate_limit_rpm,
        )
