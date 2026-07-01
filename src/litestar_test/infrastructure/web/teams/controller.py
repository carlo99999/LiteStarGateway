"""Team membership management + team-scoped API keys.

Authorization is enforced by `TeamService.ensure_can_manage_team` (platform
admin or team admin). Domain errors are mapped to HTTP by the central handler.
"""

from __future__ import annotations

from uuid import UUID

from litestar import Controller, delete, get, patch, post
from litestar.di import NamedDependency, Provide
from litestar.params import FromPath, FromQuery

from litestar_test.application.service import APIKeyService
from litestar_test.application.team_service import TeamService
from litestar_test.domain.entities import User
from litestar_test.domain.pagination import resolve_page
from litestar_test.domain.ports import UsageRepository
from litestar_test.infrastructure.web.session.dependencies import provide_current_user
from litestar_test.infrastructure.web.teams.schemas import (
    AddMemberRequest,
    CreatedKeyResponse,
    CreateKeyRequest,
    KeyResponse,
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
    ) -> list[MembershipResponse]:
        members = await team_service.list_members(current_user, team_id)
        return [MembershipResponse.from_entity(m) for m in members]

    @post("/{team_id:uuid}/members")
    async def add_member(
        self,
        team_id: FromPath[UUID],
        data: AddMemberRequest,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
    ) -> MembershipResponse:
        membership = await team_service.add_member(current_user, team_id, data.email, data.role)
        return MembershipResponse.from_entity(membership)

    @patch("/{team_id:uuid}/members/{user_id:uuid}")
    async def set_member_role(
        self,
        team_id: FromPath[UUID],
        user_id: FromPath[UUID],
        data: SetRoleRequest,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
    ) -> MembershipResponse:
        membership = await team_service.set_role(current_user, team_id, user_id, data.role)
        return MembershipResponse.from_entity(membership)

    @delete("/{team_id:uuid}/members/{user_id:uuid}")
    async def remove_member(
        self,
        team_id: FromPath[UUID],
        user_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
    ) -> None:
        await team_service.remove_member(current_user, team_id, user_id)

    @post("/{team_id:uuid}/keys")
    async def create_key(
        self,
        team_id: FromPath[UUID],
        data: CreateKeyRequest,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        api_key_service: NamedDependency[APIKeyService],
    ) -> CreatedKeyResponse:
        await team_service.ensure_can_manage_team(current_user, team_id)
        issued = await api_key_service.issue(
            team_id=team_id, created_by=current_user.id, name=data.name
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

    @delete("/{team_id:uuid}/keys/{key_id:uuid}")
    async def revoke_key(
        self,
        team_id: FromPath[UUID],
        key_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        api_key_service: NamedDependency[APIKeyService],
    ) -> None:
        await team_service.ensure_can_manage_team(current_user, team_id)
        await api_key_service.revoke_for_team(team_id, key_id)

    @get("/{team_id:uuid}/usage")
    async def usage(
        self,
        team_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        usage_repository: NamedDependency[UsageRepository],
        model: FromQuery[str | None] = None,
        api_key_id: FromQuery[UUID | None] = None,
    ) -> list[UsageResponse]:
        """Per-model token/cost totals for the team. Optional `?model=` and
        `?api_key_id=` query filters; unfiltered returns every model."""
        await team_service.ensure_can_manage_team(current_user, team_id)
        aggregates = await usage_repository.aggregate(
            team_id, model_name=model, api_key_id=api_key_id
        )
        return [UsageResponse.from_aggregate(a) for a in aggregates]
