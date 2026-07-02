"""Team membership management + team-scoped API keys.

Authorization is enforced by `TeamService.ensure_can_manage_team` (platform
admin or team admin). Domain errors are mapped to HTTP by the central handler.
"""

from __future__ import annotations

from uuid import UUID

from litestar import Controller, Request, delete, get, patch, post
from litestar.di import NamedDependency, Provide
from litestar.params import FromPath, FromQuery

from litestar_test.application.service import APIKeyService
from litestar_test.application.team_service import TeamService
from litestar_test.domain.entities import User
from litestar_test.domain.pagination import resolve_page
from litestar_test.domain.ports import AuditLog, UsageRepository
from litestar_test.infrastructure.web.audit.recorder import record_audit
from litestar_test.infrastructure.web.session.dependencies import provide_current_user
from litestar_test.infrastructure.web.teams.schemas import (
    AddMemberRequest,
    CreatedKeyResponse,
    CreateKeyRequest,
    KeyResponse,
    KeySpendingResponse,
    MembershipResponse,
    SetRoleRequest,
    UsageResponse,
)


class TeamController(Controller):
    path = "/teams"
    tags = ["teams"]
    # Any authenticated user; per-team authorization happens in the service.
    dependencies = {"current_user": Provide(provide_current_user)}

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
        await team_service.ensure_can_manage_team(current_user, team_id)
        issued = await api_key_service.issue(
            team_id=team_id, created_by=current_user.id, name=data.name
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
        await team_service.ensure_can_manage_team(current_user, team_id)
        page_limit, page_offset = resolve_page(limit, offset)
        keys = await api_key_service.list_for_team(team_id, limit=page_limit, offset=page_offset)
        return [KeyResponse.from_entity(k) for k in keys]

    @get("/{team_id:uuid}/keys/spending", summary="API keys (incl. revoked) with their spend")
    async def keys_spending(
        self,
        team_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        api_key_service: NamedDependency[APIKeyService],
        usage_repository: NamedDependency[UsageRepository],
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[KeySpendingResponse]:
        """Every API key of the team — active and revoked — with its accumulated
        token/cost totals, so past keys and their spend stay visible."""
        await team_service.ensure_can_manage_team(current_user, team_id)
        page_limit, page_offset = resolve_page(limit, offset)
        keys = await api_key_service.list_for_team(team_id, limit=page_limit, offset=page_offset)
        spend = {s.api_key_id: s for s in await usage_repository.spend_by_api_key(team_id)}
        return [KeySpendingResponse.from_key_and_spend(k, spend.get(k.id)) for k in keys]

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
        await team_service.ensure_can_manage_team(current_user, team_id)
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

    @get("/{team_id:uuid}/usage")
    async def usage(
        self,
        team_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        usage_repository: NamedDependency[UsageRepository],
        model: FromQuery[str | None] = None,
        api_key_id: FromQuery[UUID | None] = None,
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[UsageResponse]:
        """Per-model token/cost totals for the team. Optional `?model=` and
        `?api_key_id=` query filters; unfiltered returns every model, paged."""
        await team_service.ensure_can_manage_team(current_user, team_id)
        page_limit, page_offset = resolve_page(limit, offset)
        aggregates = await usage_repository.aggregate(
            team_id,
            model_name=model,
            api_key_id=api_key_id,
            limit=page_limit,
            offset=page_offset,
        )
        return [UsageResponse.from_aggregate(a) for a in aggregates]
