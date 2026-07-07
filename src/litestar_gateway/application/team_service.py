"""Application service for teams and memberships.

Authorization model (extended RBAC — see domain/authorization.py):
  * Platform admin (User.is_admin) holds every permission in every team.
  * Platform auditor (User.is_auditor) holds the read-only auditor subset
    (usage/budget) in every team.
  * Otherwise the actor's membership role grants a declared permission set:
    admin → everything in the team; member → nothing; the extended roles
    (model-manager, key-issuer, billing-viewer) → one capability domain each.
All checks funnel through `ensure_team_permission` (humans) and
`ensure_principal_team_permission` (JWT or management-scoped API key).
Only platform admins may create teams. On creation the platform admin becomes
the team's first admin, plus a named team-admin (by email); a freshly created
team therefore has two admins (one, if the named lead IS the platform admin).

Each write use-case is a unit of work: the team/membership repositories used
here only stage (flush), and the service commits once via the `Transaction`
port, so multi-step operations (e.g. team + memberships) persist atomically.
(This is the project-wide rule for multi-write use cases — see the
`Transaction` port; single-write repositories may self-commit.)
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from litestar_gateway.domain.authorization import (
    AUDITOR_TEAM_PERMISSIONS,
    Permission,
    role_grants,
)
from litestar_gateway.domain.entities import Principal, Team, TeamMembership, TeamRole, User
from litestar_gateway.domain.exceptions import (
    AlreadyMember,
    LastTeamAdmin,
    MembershipNotFound,
    OrganizationNotFound,
    PermissionDenied,
    TeamNotFound,
    UserNotFound,
)
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.domain.ports import (
    OrganizationRepository,
    TeamMembershipRepository,
    TeamRepository,
    Transaction,
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
        transaction: Transaction,
    ) -> None:
        self._orgs = organizations
        self._teams = teams
        self._memberships = memberships
        self._users = users
        self._transaction = transaction

    @asynccontextmanager
    async def _unit_of_work(self) -> AsyncGenerator[None]:
        """Commit staged writes once on success; roll back on any failure."""
        try:
            yield
            await self._transaction.commit()
        except Exception:
            await self._transaction.rollback()
            raise

    def _admin_membership(self, team_id: UUID, user_id: UUID) -> TeamMembership:
        return TeamMembership(
            id=uuid4(),
            team_id=team_id,
            user_id=user_id,
            role=TeamRole.ADMIN,
            created_at=_now(),
        )

    async def _is_last_admin(self, team_id: UUID, user_id: UUID) -> bool:
        """True if `user_id` is the team's only remaining admin, so demoting or
        removing them would leave the team with no admin. Uses an admin *count*
        rather than a page of `list_by_team`, so the invariant stays correct on
        teams with more members than one page (the M5 pagination default)."""
        membership = await self._memberships.get(team_id, user_id)
        if membership is None or not membership.is_admin:
            return False
        return await self._memberships.count_admins(team_id) == 1

    async def ensure_team_permission(
        self, actor: User, team_id: UUID, permission: Permission
    ) -> Team:
        """Return the team if `actor` holds `permission` in it, else raise.

        Platform admins hold every permission; a platform auditor holds the
        read-only auditor subset in every team; anyone else needs a membership
        whose role grants the permission (domain/authorization.py)."""
        team = await self._teams.get(team_id)
        if team is None:
            raise TeamNotFound(str(team_id))
        if actor.is_admin:
            return team
        if actor.is_auditor and permission in AUDITOR_TEAM_PERMISSIONS:
            return team
        membership = await self._memberships.get(team_id, actor.id)
        if membership is None or not role_grants(membership.role, permission):
            raise PermissionDenied(f"Requires '{permission}' in this team")
        return team

    async def ensure_principal_team_permission(
        self, principal: Principal, team_id: UUID, permission: Permission
    ) -> Team:
        """Principal-aware variant: a human goes through the user/role rules; a
        key (team service principal) acts in its own team only, and only with a
        management-capable scope — which carries every team permission, exactly
        as before roles were extended. A key is never a platform admin."""
        if principal.user is not None:
            return await self.ensure_team_permission(principal.user, team_id, permission)
        key = principal.api_key
        if key is None:  # pragma: no cover - Principal always carries one side
            raise PermissionDenied("Unauthenticated principal")
        team = await self._teams.get(team_id)
        if team is None:
            raise TeamNotFound(str(team_id))
        # Management is reserved for a key that belongs to an *enabled* service
        # principal of this team. A personal key (no SP) or a disabled SP's key
        # can never manage — regardless of the key's stored scope.
        sp = principal.service_principal
        if (
            not key.scope.allows_management
            or key.team_id != team_id
            or sp is None
            or not sp.enabled
        ):
            raise PermissionDenied("API key cannot manage this team")
        return team

    async def create_team(
        self, actor: User, organization_id: UUID, name: str, admin_email: str
    ) -> Team:
        if not actor.is_admin:
            raise PermissionDenied("Platform admin privileges required")
        if await self._orgs.get(organization_id) is None:
            raise OrganizationNotFound(str(organization_id))
        lead = await self._users.get_by_email(_normalize_email(admin_email))
        if lead is None:
            raise UserNotFound(admin_email)

        async with self._unit_of_work():
            team = await self._teams.add(
                Team(
                    id=uuid4(),
                    organization_id=organization_id,
                    name=name,
                    created_at=_now(),
                )
            )
            # The platform admin is always the team's first admin.
            await self._memberships.add(self._admin_membership(team.id, actor.id))
            # Plus the named team lead, unless they ARE the platform admin.
            if lead.id != actor.id:
                await self._memberships.add(self._admin_membership(team.id, lead.id))
        return team

    async def list_members(
        self, actor: User, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[TeamMembership]:
        await self.ensure_team_permission(actor, team_id, Permission.MEMBERS_READ)
        return await self._memberships.list_by_team(team_id, limit=limit, offset=offset)

    async def add_member(
        self, actor: User, team_id: UUID, email: str, role: TeamRole
    ) -> TeamMembership:
        await self.ensure_team_permission(actor, team_id, Permission.MEMBERS_MANAGE)
        user = await self._users.get_by_email(_normalize_email(email))
        if user is None:
            raise UserNotFound(email)
        if await self._memberships.get(team_id, user.id) is not None:
            raise AlreadyMember(email)
        async with self._unit_of_work():
            membership = await self._memberships.add(
                TeamMembership(
                    id=uuid4(),
                    team_id=team_id,
                    user_id=user.id,
                    role=role,
                    created_at=_now(),
                )
            )
        return membership

    async def set_role(
        self, actor: User, team_id: UUID, user_id: UUID, role: TeamRole
    ) -> TeamMembership:
        await self.ensure_team_permission(actor, team_id, Permission.MEMBERS_MANAGE)
        membership = await self._memberships.get(team_id, user_id)
        if membership is None:
            raise MembershipNotFound(str(user_id))
        # Demoting the sole admin would leave the team unmanageable.
        if (
            membership.is_admin
            and role is not TeamRole.ADMIN
            and await self._is_last_admin(team_id, user_id)
        ):
            raise LastTeamAdmin("Cannot demote the last admin of the team")
        async with self._unit_of_work():
            updated = await self._memberships.update(dataclasses.replace(membership, role=role))
        return updated

    async def remove_member(self, actor: User, team_id: UUID, user_id: UUID) -> None:
        await self.ensure_team_permission(actor, team_id, Permission.MEMBERS_MANAGE)
        membership = await self._memberships.get(team_id, user_id)
        if membership is None:
            raise MembershipNotFound(str(user_id))
        # Removing the sole admin would leave the team unmanageable.
        if membership.is_admin and await self._is_last_admin(team_id, user_id):
            raise LastTeamAdmin("Cannot remove the last admin of the team")
        async with self._unit_of_work():
            await self._memberships.remove(team_id, user_id)

    async def reconcile_sso_memberships(
        self,
        user_id: UUID,
        desired: dict[UUID, TeamRole],
        governed_team_ids: set[UUID],
    ) -> None:
        """Sync an SSO user's memberships to their IdP groups (no human actor).

        This is a system operation invoked from the SSO login path, so it skips
        the actor authorization checks the other mutators enforce — the caller
        derives `desired`/`governed_team_ids` from the verified id_token groups
        and the trusted SSO_TEAM_MAPPING.

        `governed_team_ids` is the mapping's codomain — every team SSO can assign.
        For those teams membership is dictated by the user's groups: add the ones
        `desired` grants, update a stale role, remove the ones no longer granted.
        Teams outside this set are never touched, so memberships added manually
        (to teams the mapping doesn't govern) survive. The last admin of a team is
        never stripped or demoted — that would leave the team unmanageable; such a
        membership is left as-is and can be changed through the normal team API.
        """
        async with self._unit_of_work():
            for team_id in governed_team_ids:
                if await self._teams.get(team_id) is None:
                    continue  # mapping references a since-deleted team
                existing = await self._memberships.get(team_id, user_id)
                role = desired.get(team_id)
                if role is None:
                    if existing is not None and not (
                        existing.is_admin and await self._is_last_admin(team_id, user_id)
                    ):
                        await self._memberships.remove(team_id, user_id)
                    continue
                if existing is None:
                    await self._memberships.add(
                        TeamMembership(
                            id=uuid4(),
                            team_id=team_id,
                            user_id=user_id,
                            role=role,
                            created_at=_now(),
                        )
                    )
                elif existing.role is not role and not (
                    existing.is_admin
                    and role is not TeamRole.ADMIN
                    and await self._is_last_admin(team_id, user_id)
                ):
                    await self._memberships.update(dataclasses.replace(existing, role=role))
