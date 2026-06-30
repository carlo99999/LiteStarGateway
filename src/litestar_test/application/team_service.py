"""Application service for teams and memberships.

Authorization model:
  * Platform admin (User.is_admin) may manage any team.
  * Team admin (membership role ADMIN) may manage only their own team.
Only platform admins may create teams; the first team-admin is named by email.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import UUID, uuid4

from litestar_test.domain.entities import Team, TeamMembership, TeamRole, User
from litestar_test.domain.exceptions import (
    AlreadyMember,
    MembershipNotFound,
    OrganizationNotFound,
    PermissionDenied,
    TeamNotFound,
    UserNotFound,
)
from litestar_test.domain.ports import (
    OrganizationRepository,
    TeamMembershipRepository,
    TeamRepository,
    UserRepository,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


class TeamService:
    def __init__(
        self,
        organizations: OrganizationRepository,
        teams: TeamRepository,
        memberships: TeamMembershipRepository,
        users: UserRepository,
    ) -> None:
        self._orgs = organizations
        self._teams = teams
        self._memberships = memberships
        self._users = users

    async def ensure_can_manage_team(self, actor: User, team_id: UUID) -> Team:
        """Return the team if `actor` may manage it, else raise."""
        team = await self._teams.get(team_id)
        if team is None:
            raise TeamNotFound(str(team_id))
        if actor.is_admin:
            return team
        membership = await self._memberships.get(team_id, actor.id)
        if membership is None or not membership.is_admin:
            raise PermissionDenied("Team admin privileges required")
        return team

    async def create_team(
        self, actor: User, organization_id: UUID, name: str, admin_email: str
    ) -> Team:
        if not actor.is_admin:
            raise PermissionDenied("Platform admin privileges required")
        if await self._orgs.get(organization_id) is None:
            raise OrganizationNotFound(str(organization_id))
        admin = await self._users.get_by_email(_normalize_email(admin_email))
        if admin is None:
            raise UserNotFound(admin_email)

        team = await self._teams.add(
            Team(
                id=uuid4(),
                organization_id=organization_id,
                name=name,
                created_at=_now(),
            )
        )
        await self._memberships.add(
            TeamMembership(
                id=uuid4(),
                team_id=team.id,
                user_id=admin.id,
                role=TeamRole.ADMIN,
                created_at=_now(),
            )
        )
        return team

    async def list_members(self, actor: User, team_id: UUID) -> list[TeamMembership]:
        await self.ensure_can_manage_team(actor, team_id)
        return await self._memberships.list_by_team(team_id)

    async def add_member(
        self, actor: User, team_id: UUID, email: str, role: TeamRole
    ) -> TeamMembership:
        await self.ensure_can_manage_team(actor, team_id)
        user = await self._users.get_by_email(_normalize_email(email))
        if user is None:
            raise UserNotFound(email)
        if await self._memberships.get(team_id, user.id) is not None:
            raise AlreadyMember(email)
        return await self._memberships.add(
            TeamMembership(
                id=uuid4(),
                team_id=team_id,
                user_id=user.id,
                role=role,
                created_at=_now(),
            )
        )

    async def set_role(
        self, actor: User, team_id: UUID, user_id: UUID, role: TeamRole
    ) -> TeamMembership:
        await self.ensure_can_manage_team(actor, team_id)
        membership = await self._memberships.get(team_id, user_id)
        if membership is None:
            raise MembershipNotFound(str(user_id))
        return await self._memberships.update(dataclasses.replace(membership, role=role))

    async def remove_member(self, actor: User, team_id: UUID, user_id: UUID) -> None:
        await self.ensure_can_manage_team(actor, team_id)
        if await self._memberships.get(team_id, user_id) is None:
            raise MembershipNotFound(str(user_id))
        await self._memberships.remove(team_id, user_id)
