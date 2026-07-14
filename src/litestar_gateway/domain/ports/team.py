"""Port — team and membership persistence."""

from __future__ import annotations

from collections.abc import Sequence
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

    async def update(
        self,
        team_id: UUID,
        name: str,
        description: str | None,
        tags: Sequence[str],
        rate_limit_rpm: int | None,
    ) -> Team | None:
        """Replace the team's editable metadata (name, description, tags,
        rate_limit_rpm); return the updated entity, or None if none has that id."""
        ...

    async def delete(self, team_id: UUID) -> None:
        """Delete the team and its intrinsic children (memberships, budget,
        routers, service principals, and usage history). The caller is
        responsible for refusing when models or API keys still exist — those are
        NOT removed here."""
        ...


class TeamMembershipRepository(Protocol):
    """Persistence port for team memberships."""

    async def add(self, membership: TeamMembership) -> TeamMembership: ...

    async def get(self, team_id: UUID, user_id: UUID) -> TeamMembership | None: ...

    async def list_by_team(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[TeamMembership]: ...

    async def list_by_user(
        self, user_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[TeamMembership]:
        """The user's memberships across all teams — used to guard user deletion."""
        ...

    async def count_admins(self, team_id: UUID) -> int:
        """Number of admin memberships on the team. Unpaginated on purpose: the
        last-admin invariant must see every admin, not just the first page."""
        ...

    async def update(self, membership: TeamMembership) -> TeamMembership: ...

    async def remove(self, team_id: UUID, user_id: UUID) -> None: ...
