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
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from litestar_gateway.domain.authorization import (
    AUDITOR_TEAM_PERMISSIONS,
    Permission,
    role_grants,
)
from litestar_gateway.domain.entities import (
    AuditEvent,
    Principal,
    Team,
    TeamMembership,
    TeamRole,
    User,
)
from litestar_gateway.domain.exceptions import (
    AlreadyMember,
    LastTeamAdmin,
    MembershipNotFound,
    OrganizationNotFound,
    PermissionDenied,
    TeamNotEmpty,
    TeamNotFound,
    TeamNotSoftDeleted,
    UserNotFound,
)
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.domain.ports import (
    APIKeyRepository,
    AuditLog,
    ModelRepository,
    OrganizationRepository,
    RoutingDecisionLog,
    TeamMembershipRepository,
    TeamRepository,
    Transaction,
    UsageRepository,
    UserRepository,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


@dataclasses.dataclass(frozen=True)
class SsoMembershipChange:
    """One mutation applied by `reconcile_sso_memberships`, reported so the SSO
    callback can audit it (no-op reconciliations report nothing) — R6-H20."""

    team_id: UUID
    change: Literal["add", "update", "remove"]
    role: TeamRole | None  # the new role; None for removals


class TeamService:
    def __init__(
        self,
        organizations: OrganizationRepository,
        teams: TeamRepository,
        memberships: TeamMembershipRepository,
        users: UserRepository,
        transaction: Transaction,
        models: ModelRepository,
        api_keys: APIKeyRepository,
        *,
        usage: UsageRepository | None = None,
        audit_log: AuditLog | None = None,
        routing: RoutingDecisionLog | None = None,
    ) -> None:
        self._orgs = organizations
        self._teams = teams
        self._memberships = memberships
        self._users = users
        self._transaction = transaction
        self._models = models
        self._api_keys = api_keys
        # Optional: only export/purge (Plan 13 Phase 5) need these, so existing
        # callers/tests that build a TeamService without them keep working.
        self._usage = usage
        self._audit = audit_log
        self._routing = routing

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

    async def principal_has_team_permission(
        self, principal: Principal, team_id: UUID, permission: Permission
    ) -> bool:
        """Non-raising variant of `ensure_principal_team_permission`, for
        endpoints that adapt their response to the caller's permissions
        (e.g. redact fields) instead of rejecting the request outright."""
        try:
            await self.ensure_principal_team_permission(principal, team_id, permission)
        except PermissionDenied:
            return False
        return True

    async def create_team(
        self,
        actor: User,
        organization_id: UUID,
        name: str,
        admin_email: str,
        *,
        description: str | None = None,
        tags: Sequence[str] | None = None,
        rate_limit_rpm: int | None = None,
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
                    description=description,
                    tags=list(tags or []),
                    rate_limit_rpm=rate_limit_rpm,
                )
            )
            # The platform admin is always the team's first admin.
            await self._memberships.add(self._admin_membership(team.id, actor.id))
            # Plus the named team lead, unless they ARE the platform admin.
            if lead.id != actor.id:
                await self._memberships.add(self._admin_membership(team.id, lead.id))
        return team

    async def list_all_teams(
        self, actor: User, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Team]:
        """Every team across all organizations — platform-admin only (the admin
        console's global teams view)."""
        if not actor.is_admin:
            raise PermissionDenied("Platform admin privileges required")
        return await self._teams.list(limit=limit, offset=offset)

    async def get_team(self, actor: User, team_id: UUID) -> Team:
        """One team by id, authorized like reading its members (platform admin,
        platform auditor, or a team member with read)."""
        return await self.ensure_team_permission(actor, team_id, Permission.MEMBERS_READ)

    async def update_team(
        self,
        actor: User,
        team_id: UUID,
        name: str,
        *,
        description: str | None = None,
        tags: Sequence[str] | None = None,
        rate_limit_rpm: int | None = None,
    ) -> Team:
        """Update a team's name/description/tags/rate limit — platform-admin only
        (a structural op, like create)."""
        if not actor.is_admin:
            raise PermissionDenied("Platform admin privileges required")
        async with self._unit_of_work():
            team = await self._teams.update(
                team_id, name, description, list(tags or []), rate_limit_rpm
            )
        if team is None:
            raise TeamNotFound(str(team_id))
        return team

    async def delete_team(self, actor: User, team_id: UUID) -> Team:
        """Delete a team — platform-admin only. Refuses (TeamNotEmpty → 409) if it
        still has models or API keys.

        A team with NO billed history (no usage_event/pending_usage_event row) is
        hard-deleted exactly as before, with its intrinsic children (members,
        budget, routers, service principals, invites). A team WITH billed
        history is soft-deleted (tombstoned) instead: it disappears from normal
        listings/operations, but its usage/routing/audit history stays intact
        and queryable until the separate, audited `purge_team` action removes it
        for good (Plan 13 Phase 5). Returns the (pre-delete, or now-tombstoned)
        team for the caller's audit trail."""
        if not actor.is_admin:
            raise PermissionDenied("Platform admin privileges required")
        async with self._unit_of_work():
            team = await self._teams.lock_for_lifecycle(team_id)
            if team is None:
                raise TeamNotFound(str(team_id))
            if await self._models.list_by_team(team_id, limit=1, offset=0):
                raise TeamNotEmpty(str(team_id))
            if await self._api_keys.list_by_team(team_id, limit=1, offset=0):
                raise TeamNotEmpty(str(team_id))
            if await self._teams.has_billed_history(team_id):
                tombstoned = await self._teams.soft_delete(team_id)
                return tombstoned if tombstoned is not None else team
            await self._teams.delete(team_id)
        return team

    async def export_team_data(self, actor: User, team_id: UUID) -> dict[str, Any]:
        """Full usage/audit history for one team, as a JSON-serializable dict —
        the export-before-delete workflow (Plan 13 Phase 5). Platform-admin
        only; works on a live OR already soft-deleted team (an admin exporting
        right before purge must still be able to reach a tombstoned team)."""
        if not actor.is_admin:
            raise PermissionDenied("Platform admin privileges required")
        team = await self._teams.get_any(team_id)
        if team is None:
            raise TeamNotFound(str(team_id))
        usage_events: list[Any] = []
        if self._usage is not None:
            offset = 0
            while True:
                page = await self._usage.list_events(
                    team_id, limit=DEFAULT_PAGE_SIZE, offset=offset
                )
                usage_events.extend(page)
                if len(page) < DEFAULT_PAGE_SIZE:
                    break
                offset += len(page)
        audit_events: list[Any] = []
        if self._audit is not None:
            offset = 0
            while True:
                page = await self._audit.list_by_target(
                    "team", str(team_id), limit=DEFAULT_PAGE_SIZE, offset=offset
                )
                audit_events.extend(page)
                if len(page) < DEFAULT_PAGE_SIZE:
                    break
                offset += len(page)
        # Routing history is exported as the same savings aggregate the console
        # already shows for a team, not a raw per-decision dump: routing
        # decisions have no FK to team and no team-wide raw query exists yet
        # (only per-router `list_decisions`), so a complete raw export is left
        # for a follow-up; the aggregate keeps this export honest about what it
        # actually contains.
        routing_savings = None
        if self._routing is not None:
            (
                total_savings,
                decisions_counted,
                decisions_without_usage,
            ) = await self._routing.team_savings(team_id)
            routing_savings = {
                "total_estimated_savings": total_savings,
                "decisions_counted": decisions_counted,
                "decisions_without_usage": decisions_without_usage,
            }
        return {
            "team": team,
            "usage_events": usage_events,
            "audit_events": audit_events,
            "routing_savings": routing_savings,
        }

    async def purge_team(self, actor: User, team_id: UUID, audit_event: AuditEvent) -> Team:
        """Irreversibly remove a soft-deleted team's data — the separate,
        explicit, audited purge action (Plan 13 Phase 5). Platform-admin only,
        and only on a team already tombstoned by `delete_team`; a live team
        must go through the tombstone step first (→ 409 TeamNotSoftDeleted).

        `audit_event` is staged in the SAME transaction as the deletion, before
        it, so a crash mid-purge can never leave the destructive action
        unaudited: either both the audit record and the deletion commit, or
        neither does. Audit records themselves are never removed by purge —
        they are the forensic evidence that the purge happened, and outlive it
        (see docs/next-steps/billing-integrity.md §5)."""
        if not actor.is_admin:
            raise PermissionDenied("Platform admin privileges required")
        async with self._unit_of_work():
            team = await self._teams.get_any(team_id)
            if team is None:
                raise TeamNotFound(str(team_id))
            if team.deleted_at is None:
                raise TeamNotSoftDeleted(str(team_id))
            if self._audit is None:  # pragma: no cover - wiring invariant
                raise RuntimeError("Audit log is required to purge a team")
            await self._audit.stage(audit_event)
            await self._teams.delete(team_id)
        return team

    async def list_members(
        self, actor: User, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[TeamMembership]:
        await self.ensure_team_permission(actor, team_id, Permission.MEMBERS_READ)
        return await self._memberships.list_by_team(team_id, limit=limit, offset=offset)

    async def list_user_teams(self, user: User) -> list[tuple[Team, TeamRole]]:
        """Every team `user` belongs to, with their role there. Self-scoped: the
        caller only ever sees their own memberships, so no permission gate.

        Pages through all memberships (the "my teams" contract is complete, not
        first-page-only) and resolves the teams in one batch query (no N+1)."""
        memberships: list[TeamMembership] = []
        offset = 0
        while True:
            page = await self._memberships.list_by_user(
                user.id, limit=DEFAULT_PAGE_SIZE, offset=offset
            )
            memberships.extend(page)
            if len(page) < DEFAULT_PAGE_SIZE:
                break
            offset += len(page)
        teams_by_id = {
            team.id: team
            for team in await self._teams.list_by_ids([m.team_id for m in memberships])
        }
        return [(teams_by_id[m.team_id], m.role) for m in memberships if m.team_id in teams_by_id]

    async def add_member(
        self, actor: User, team_id: UUID, email: str, role: TeamRole
    ) -> TeamMembership:
        await self.ensure_team_permission(actor, team_id, Permission.MEMBERS_MANAGE)
        user = await self._users.get_by_email(_normalize_email(email))
        if user is None:
            raise UserNotFound(email)
        if await self._memberships.get(team_id, user.id) is not None:
            raise AlreadyMember(email)
        try:
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
        except AlreadyMember:
            # The adapter maps *any* insert IntegrityError to AlreadyMember, but
            # it can also be an FK violation from a concurrent deletion: the
            # user (or team) vanished between the pre-check and the insert.
            # Re-read committed state to report the real cause instead of a
            # misleading 409 "already a member" (round 10 ISSUE-006).
            if await self._users.get(user.id) is None:
                raise UserNotFound(email) from None
            if await self._teams.get(team_id) is None:
                raise TeamNotFound(str(team_id)) from None
            raise
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
    ) -> list[SsoMembershipChange]:
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

        Returns the changes actually applied so the caller can audit them.
        """
        changes: list[SsoMembershipChange] = []
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
                        changes.append(SsoMembershipChange(team_id, "remove", None))
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
                    changes.append(SsoMembershipChange(team_id, "add", role))
                elif existing.role is not role and not (
                    existing.is_admin
                    and role is not TeamRole.ADMIN
                    and await self._is_last_admin(team_id, user_id)
                ):
                    await self._memberships.update(dataclasses.replace(existing, role=role))
                    changes.append(SsoMembershipChange(team_id, "update", role))
        return changes
