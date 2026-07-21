"""Team membership management + team-scoped API keys.

Authorization is enforced by `TeamService.ensure_team_permission` (platform
admin or team admin). Domain errors are mapped to HTTP by the central handler.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from litestar import Controller, Request, delete, get, patch, post, put
from litestar.di import NamedDependency, Provide
from litestar.params import FromPath, FromQuery

from litestar_gateway.application.service import APIKeyService
from litestar_gateway.application.team_service import TeamService
from litestar_gateway.domain.authorization import Permission
from litestar_gateway.domain.budget import window_start
from litestar_gateway.domain.entities import Budget, BudgetWindow, KeyScope, Principal, User
from litestar_gateway.domain.exceptions import BudgetNotFound, InvalidBudget, InvalidKeyScope
from litestar_gateway.domain.pagination import resolve_page
from litestar_gateway.domain.ports import AuditLog, BudgetRepository, UsageRepository
from litestar_gateway.infrastructure.web.audit.recorder import make_audit_event, record_audit
from litestar_gateway.infrastructure.web.principal import provide_principal
from litestar_gateway.infrastructure.web.session.dependencies import (
    provide_current_admin,
    provide_current_user,
)
from litestar_gateway.infrastructure.web.teams.schemas import (
    AddMemberRequest,
    BudgetResponse,
    CreatedKeyResponse,
    CreateKeyRequest,
    KeyResponse,
    KeySpendingResponse,
    MembershipResponse,
    SetBudgetRequest,
    SetRoleRequest,
    TeamResponse,
    UpdateTeamRequest,
    UsageResponse,
    resolve_key_expiry,
)


def _parse_budget(data: SetBudgetRequest, team_id: UUID) -> Budget:
    if data.limit_cost <= 0:
        raise InvalidBudget("limit_cost must be a positive USD amount")
    try:
        window = BudgetWindow(data.window)
    except ValueError:
        valid = ", ".join(w.value for w in BudgetWindow)
        raise InvalidBudget(f"window must be one of: {valid}") from None
    return Budget(
        id=uuid4(),
        team_id=team_id,
        limit_cost=data.limit_cost,
        window=window,
        created_at=datetime.now(UTC),
    )


class TeamController(Controller):
    path = "/teams"
    tags = ["teams"]
    # Any authenticated user; per-team authorization happens in the service.
    dependencies = {"current_user": Provide(provide_current_user)}

    @get("")
    async def list_teams(
        self,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[TeamResponse]:
        """Every team across all organizations (platform-admin only)."""
        page_limit, page_offset = resolve_page(limit, offset)
        teams = await team_service.list_all_teams(
            current_user, limit=page_limit, offset=page_offset
        )
        return [TeamResponse.from_entity(t) for t in teams]

    @get("/{team_id:uuid}")
    async def get_team(
        self,
        team_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
    ) -> TeamResponse:
        """One team by id (platform admin, auditor, or a team member with read)."""
        team = await team_service.get_team(current_user, team_id)
        return TeamResponse.from_entity(team)

    @patch("/{team_id:uuid}", dependencies={"current_admin": Provide(provide_current_admin)})
    async def update_team(
        self,
        request: Request,
        team_id: FromPath[UUID],
        data: UpdateTeamRequest,
        current_admin: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        audit_log: NamedDependency[AuditLog],
    ) -> TeamResponse:
        team = await team_service.update_team(
            current_admin,
            team_id,
            data.name,
            description=data.description,
            tags=data.tags,
            rate_limit_rpm=data.rate_limit_rpm,
        )
        await record_audit(
            audit_log,
            request,
            current_admin,
            "team.update",
            target_type="team",
            target_id=team.id,
            detail=data.name,
        )
        return TeamResponse.from_entity(team)

    @delete("/{team_id:uuid}", dependencies={"current_admin": Provide(provide_current_admin)})
    async def delete_team(
        self,
        request: Request,
        team_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        # Refuses with 409 (TeamNotEmpty) if the team still has models or API
        # keys; otherwise removes the team and its intrinsic children.
        team = await team_service.delete_team(current_admin, team_id)
        await record_audit(
            audit_log,
            request,
            current_admin,
            "team.delete",
            target_type="team",
            target_id=team.id,
            detail=team.name,
        )

    @get("/{team_id:uuid}/members")
    async def list_members(
        self,
        team_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[MembershipResponse]:
        page_limit, page_offset = resolve_page(limit, offset)
        members = await team_service.list_members(
            current_user, team_id, limit=page_limit, offset=page_offset
        )
        return [MembershipResponse.from_entity(m) for m in members]

    @post("/{team_id:uuid}/members")
    async def add_member(
        self,
        request: Request,
        team_id: FromPath[UUID],
        data: AddMemberRequest,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        audit_log: NamedDependency[AuditLog],
    ) -> MembershipResponse:
        membership = await team_service.add_member(current_user, team_id, data.email, data.role)
        await record_audit(
            audit_log,
            request,
            current_user,
            "team.member.add",
            target_type="team",
            target_id=team_id,
            detail=f"{data.email} as {data.role}",
        )
        return MembershipResponse.from_entity(membership)

    @patch("/{team_id:uuid}/members/{user_id:uuid}")
    async def set_member_role(
        self,
        request: Request,
        team_id: FromPath[UUID],
        user_id: FromPath[UUID],
        data: SetRoleRequest,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        audit_log: NamedDependency[AuditLog],
    ) -> MembershipResponse:
        membership = await team_service.set_role(current_user, team_id, user_id, data.role)
        await record_audit(
            audit_log,
            request,
            current_user,
            "team.member.set_role",
            target_type="team",
            target_id=team_id,
            detail=f"user {user_id} -> {data.role}",
        )
        return MembershipResponse.from_entity(membership)

    @delete("/{team_id:uuid}/members/{user_id:uuid}")
    async def remove_member(
        self,
        request: Request,
        team_id: FromPath[UUID],
        user_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        await team_service.remove_member(current_user, team_id, user_id)
        await record_audit(
            audit_log,
            request,
            current_user,
            "team.member.remove",
            target_type="team",
            target_id=team_id,
            detail=f"user {user_id}",
        )

    @post("/{team_id:uuid}/keys")
    async def create_key(
        self,
        request: Request,
        team_id: FromPath[UUID],
        data: CreateKeyRequest,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        api_key_service: NamedDependency[APIKeyService],
        audit_log: NamedDependency[AuditLog],
    ) -> CreatedKeyResponse:
        await team_service.ensure_team_permission(current_user, team_id, Permission.KEYS_ISSUE)
        try:
            scope = KeyScope(data.scope)
        except ValueError:
            valid = ", ".join(s.value for s in KeyScope)
            raise InvalidKeyScope(f"scope must be one of: {valid}") from None
        issued = await api_key_service.issue(
            team_id=team_id,
            created_by=current_user.id,
            name=data.name,
            scope=scope,
            rate_limit_rpm=data.rate_limit_rpm,
            expires_at=resolve_key_expiry(data.expires_in_days),
        )
        await record_audit(
            audit_log,
            request,
            current_user,
            "api_key.create",
            target_type="api_key",
            target_id=issued.key.id,
            detail=f"team {team_id}",
        )
        return CreatedKeyResponse.from_issued(issued)

    @get("/{team_id:uuid}/keys")
    async def list_keys(
        self,
        team_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        api_key_service: NamedDependency[APIKeyService],
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[KeyResponse]:
        await team_service.ensure_team_permission(current_user, team_id, Permission.KEYS_READ)
        page_limit, page_offset = resolve_page(limit, offset)
        keys = await api_key_service.list_for_team(team_id, limit=page_limit, offset=page_offset)
        return [KeyResponse.from_entity(k) for k in keys]

    @get(
        "/{team_id:uuid}/keys/spending",
        summary="API keys (incl. revoked) with their spend",
        dependencies={"principal": Provide(provide_principal)},
    )
    async def keys_spending(
        self,
        team_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        team_service: NamedDependency[TeamService],
        api_key_service: NamedDependency[APIKeyService],
        usage_repository: NamedDependency[UsageRepository],
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[KeySpendingResponse]:
        """Every API key of the team — active and revoked — with its accumulated
        token/cost totals, so past keys and their spend stay visible.
        Accepts a JWT or a management-scoped API key (own team only).
        Callers without keys:read get the key-identity block redacted (R6-M43)."""
        await team_service.ensure_principal_team_permission(
            principal, team_id, Permission.USAGE_READ
        )
        include_identity = await team_service.principal_has_team_permission(
            principal, team_id, Permission.KEYS_READ
        )
        page_limit, page_offset = resolve_page(limit, offset)
        keys = await api_key_service.list_for_team(team_id, limit=page_limit, offset=page_offset)
        spend = {s.api_key_id: s for s in await usage_repository.spend_by_api_key(team_id)}
        return [
            KeySpendingResponse.from_key_and_spend(
                k, spend.get(k.id), include_identity=include_identity
            )
            for k in keys
        ]

    @delete("/{team_id:uuid}/keys/{key_id:uuid}")
    async def revoke_key(
        self,
        request: Request,
        team_id: FromPath[UUID],
        key_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        api_key_service: NamedDependency[APIKeyService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        await team_service.ensure_team_permission(current_user, team_id, Permission.KEYS_ISSUE)
        await api_key_service.revoke_for_team(team_id, key_id)
        await record_audit(
            audit_log,
            request,
            current_user,
            "api_key.revoke",
            target_type="api_key",
            target_id=key_id,
            detail=f"team {team_id}",
        )

    @post("/{team_id:uuid}/keys/{key_id:uuid}/rotate")
    async def rotate_key(
        self,
        request: Request,
        team_id: FromPath[UUID],
        key_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        api_key_service: NamedDependency[APIKeyService],
    ) -> CreatedKeyResponse:
        """Issue a replacement key (same scope/rate-limit/owner) and give the old
        one a grace window before it stops working. Returns the new plaintext once."""
        await team_service.ensure_team_permission(current_user, team_id, Permission.KEYS_ISSUE)
        key = await api_key_service.get_active_for_team(team_id, key_id)
        if key.is_service_principal:
            await team_service.ensure_team_permission(
                current_user, team_id, Permission.SERVICE_PRINCIPALS_MANAGE
            )
        audit_event = make_audit_event(
            request,
            current_user,
            "api_key.rotate",
            target_type="api_key",
            target_id=key_id,
            detail=f"team {team_id}",
        )
        issued = await api_key_service.rotate_for_team(team_id, key_id, audit_event=audit_event)
        return CreatedKeyResponse.from_issued(issued)

    @get("/{team_id:uuid}/budget", dependencies={"principal": Provide(provide_principal)})
    async def get_budget(
        self,
        team_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        team_service: NamedDependency[TeamService],
        budget_repository: NamedDependency[BudgetRepository],
        usage_repository: NamedDependency[UsageRepository],
    ) -> BudgetResponse:
        """The team's spend cap plus its current-window spend and remainder.
        Accepts a JWT or a management-scoped API key (own team only)."""
        await team_service.ensure_principal_team_permission(
            principal, team_id, Permission.BUDGET_READ
        )
        budget = await budget_repository.get(team_id)
        if budget is None:
            raise BudgetNotFound(f"Team {team_id} has no budget configured")
        spent = await usage_repository.spend_since(
            team_id, window_start(budget.window, datetime.now(UTC))
        )
        return BudgetResponse.from_budget(budget, spent)

    @put(
        "/{team_id:uuid}/budget",
        dependencies={"current_admin": Provide(provide_current_admin)},
    )
    async def set_budget(
        self,
        request: Request,
        team_id: FromPath[UUID],
        data: SetBudgetRequest,
        current_admin: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        budget_repository: NamedDependency[BudgetRepository],
        usage_repository: NamedDependency[UsageRepository],
        audit_log: NamedDependency[AuditLog],
    ) -> BudgetResponse:
        """Create or replace the team's spend cap. Platform-admin only — a team
        admin must not be able to raise their own limit."""
        await team_service.ensure_team_permission(
            current_admin, team_id, Permission.BUDGET_READ
        )  # team must exist
        budget = await budget_repository.set(_parse_budget(data, team_id))
        await record_audit(
            audit_log,
            request,
            current_admin,
            "team.budget.set",
            target_type="team",
            target_id=team_id,
            detail=f"{budget.limit_cost} USD / {budget.window.value}",
        )
        spent = await usage_repository.spend_since(
            team_id, window_start(budget.window, datetime.now(UTC))
        )
        return BudgetResponse.from_budget(budget, spent)

    @delete(
        "/{team_id:uuid}/budget",
        dependencies={"current_admin": Provide(provide_current_admin)},
    )
    async def delete_budget(
        self,
        request: Request,
        team_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        budget_repository: NamedDependency[BudgetRepository],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        """Remove the team's spend cap. Platform-admin only."""
        await team_service.ensure_team_permission(current_admin, team_id, Permission.BUDGET_READ)
        await budget_repository.remove(team_id)
        await record_audit(
            audit_log,
            request,
            current_admin,
            "team.budget.remove",
            target_type="team",
            target_id=team_id,
        )

    @get("/{team_id:uuid}/usage", dependencies={"principal": Provide(provide_principal)})
    async def usage(
        self,
        team_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        team_service: NamedDependency[TeamService],
        usage_repository: NamedDependency[UsageRepository],
        model: FromQuery[str | None] = None,
        api_key_id: FromQuery[UUID | None] = None,
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[UsageResponse]:
        """Per-model token/cost totals for the team. Optional `?model=` and
        `?api_key_id=` query filters; unfiltered returns every model, paged.
        Accepts a JWT or a management-scoped API key (own team only)."""
        await team_service.ensure_principal_team_permission(
            principal, team_id, Permission.USAGE_READ
        )
        page_limit, page_offset = resolve_page(limit, offset)
        aggregates = await usage_repository.aggregate(
            team_id,
            model_name=model,
            api_key_id=api_key_id,
            limit=page_limit,
            offset=page_offset,
        )
        return [UsageResponse.from_aggregate(a) for a in aggregates]
