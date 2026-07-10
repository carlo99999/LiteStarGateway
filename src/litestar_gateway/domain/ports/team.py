"""Port — team and membership persistence."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from litestar_gateway.domain.entities import Team, TeamMembership
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE


class TeamRepository(Protocol):
    """Persistence port for teams."""

    async def add(self, team: Team) -> Team: ...

    async def get(self, team_id: UUID) -> Team | None: ...

    async def list_by_organization(
        self, organization_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Team]: ...

    async def list(self, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> list[Team]:
        """Every team across all organizations, stable order (platform-admin use)."""
        ...


class TeamMembershipRepository(Protocol):
    """Persistence port for team memberships."""

    async def add(self, membership: TeamMembership) -> TeamMembership: ...

    async def get(self, team_id: UUID, user_id: UUID) -> TeamMembership | None: ...

    async def list_by_team(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[TeamMembership]: ...

    async def count_admins(self, team_id: UUID) -> int:
        """Number of admin memberships on the team. Unpaginated on purpose: the
        last-admin invariant must see every admin, not just the first page."""
        ...

    async def update(self, membership: TeamMembership) -> TeamMembership: ...

    async def remove(self, team_id: UUID, user_id: UUID) -> None: ...
