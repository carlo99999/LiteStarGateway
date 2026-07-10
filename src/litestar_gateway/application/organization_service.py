"""Application service for organizations (platform-admin only)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from litestar_gateway.domain.entities import Organization, Team, User
from litestar_gateway.domain.exceptions import (
    OrganizationNotEmpty,
    OrganizationNotFound,
    PermissionDenied,
)
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.domain.ports import (
    OrganizationRepository,
    TeamRepository,
    UsageRepository,
)


@dataclass(frozen=True)
class TeamSpend:
    """One team's recorded cost over the rollup window."""

    team: Team
    cost: float


@dataclass(frozen=True)
class OrganizationSpend:
    """An organization's spend since `since`: the total plus a per-team breakdown."""

    since: datetime
    total_cost: float
    teams: list[TeamSpend]


def _now() -> datetime:
    return datetime.now(UTC)


def _require_platform_admin(actor: User) -> None:
    if not actor.is_admin:
        raise PermissionDenied("Platform admin privileges required")


class OrganizationService:
    def __init__(
        self,
        organizations: OrganizationRepository,
        teams: TeamRepository,
        usage: UsageRepository,
    ) -> None:
        self._orgs = organizations
        self._teams = teams
        self._usage = usage

    async def create(self, actor: User, name: str) -> Organization:
        _require_platform_admin(actor)
        return await self._orgs.add(Organization(id=uuid4(), name=name, created_at=_now()))

    async def get(self, actor: User, organization_id: UUID) -> Organization:
        _require_platform_admin(actor)
        org = await self._orgs.get(organization_id)
        if org is None:
            raise OrganizationNotFound(str(organization_id))
        return org

    async def _all_teams(self, organization_id: UUID) -> list[Team]:
        """Every team in the org, walking the paginated repo query to completion —
        the spend rollup must sum all teams, not just the first page."""
        teams: list[Team] = []
        offset = 0
        while True:
            page = await self._teams.list_by_organization(
                organization_id, limit=DEFAULT_PAGE_SIZE, offset=offset
            )
            teams.extend(page)
            if len(page) < DEFAULT_PAGE_SIZE:
                return teams
            offset += DEFAULT_PAGE_SIZE

    async def spend_rollup(
        self, actor: User, organization_id: UUID, *, since: datetime
    ) -> OrganizationSpend:
        """Total recorded cost since `since`, summed across the org's teams, with a
        per-team breakdown. Read-only reporting: reuses the same indexed
        `spend_since` aggregate the budget gate uses, one query per team."""
        _require_platform_admin(actor)
        if await self._orgs.get(organization_id) is None:
            raise OrganizationNotFound(str(organization_id))
        per_team: list[TeamSpend] = []
        total = 0.0
        for team in await self._all_teams(organization_id):
            cost = await self._usage.spend_since(team.id, since)
            per_team.append(TeamSpend(team=team, cost=cost))
            total += cost
        return OrganizationSpend(since=since, total_cost=total, teams=per_team)

    # Defined after the list[...]-returning methods above: a method named `list`
    # shadows the builtin `list` for annotations in later methods, so keep those
    # methods ahead of it.
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
