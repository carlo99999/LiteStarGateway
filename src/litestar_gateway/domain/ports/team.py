"""Port — team and membership persistence."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from litestar_gateway.domain.entities import Team, TeamMembership
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE


class TeamLifecycleRepository(Protocol):
    """Narrow port for operations serialized against team deletion."""

    async def lock_for_lifecycle(self, team_id: UUID) -> Team | None:
        """Serialize invite creation and team deletion for this team.

        Return the current team, or None if it no longer exists. The lock is
        held until the surrounding transaction completes.
        """
        ...


class TeamRepository(TeamLifecycleRepository, Protocol):
    """Persistence port for teams.

    ``get``/``list``/``list_by_organization``/``list_by_ids`` all hide
    soft-deleted (tombstoned) teams — they read like the team no longer
    exists. ``get_any`` is the one exception: it bypasses the tombstone
    filter, for the purge flow and the export action, which must still be
    able to reach a soft-deleted team.
    """

    async def add(self, team: Team) -> Team: ...

    async def get(self, team_id: UUID) -> Team | None: ...

    async def get_any(self, team_id: UUID) -> Team | None:
        """Like `get`, but also returns a soft-deleted team (purge/export)."""
        ...

    async def has_billed_history(self, team_id: UUID) -> bool:
        """True if the team has any usage_event or pending_usage_event row —
        the "billed history" test that decides soft- vs. hard-delete."""
        ...

    async def soft_delete(self, team_id: UUID) -> Team | None:
        """Tombstone the team (set `deleted_at`) instead of removing it.
        Returns the updated entity, or None if the team no longer exists."""
        ...

    async def list_by_ids(self, team_ids: Sequence[UUID]) -> list[Team]:
        """Fetch many teams in one query (batch lookup, avoids N+1). Missing ids
        are simply absent from the result; order is not guaranteed."""
        ...

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
        """Hard-delete the team and its intrinsic children (memberships, budget,
        routers, service principals, invites, and usage history). The caller is
        responsible for refusing when models or API keys still exist — those are
        NOT removed here.

        Two, and only two, callers may invoke this: the ordinary team-delete
        path when the team has NO billed history (regression-safe fast path),
        and the explicit, audited purge action on an already soft-deleted team.
        Any other caller risks the accidental destructive-cascade this port is
        designed to prevent — use `soft_delete` instead."""
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
