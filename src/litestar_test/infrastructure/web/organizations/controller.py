"""Organization & team creation — platform-admin only."""

from __future__ import annotations

from uuid import UUID

from litestar import Controller, get, post
from litestar.di import NamedDependency, Provide
from litestar.params import FromPath, FromQuery

from litestar_test.application.organization_service import OrganizationService
from litestar_test.application.team_service import TeamService
from litestar_test.domain.entities import User
from litestar_test.domain.pagination import resolve_page
from litestar_test.infrastructure.web.organizations.schemas import (
    CreateOrganizationRequest,
    OrganizationResponse,
)
from litestar_test.infrastructure.web.session.dependencies import provide_current_admin
from litestar_test.infrastructure.web.teams.schemas import CreateTeamRequest, TeamResponse


class OrganizationController(Controller):
    path = "/organizations"
    tags = ["organizations"]
    # Every route here requires a platform admin (User.is_admin).
    dependencies = {"current_admin": Provide(provide_current_admin)}

    @post()
    async def create_organization(
        self,
        data: CreateOrganizationRequest,
        current_admin: NamedDependency[User],
        organization_service: NamedDependency[OrganizationService],
    ) -> OrganizationResponse:
        org = await organization_service.create(current_admin, data.name)
        return OrganizationResponse.from_entity(org)

    @get()
    async def list_organizations(
        self,
        current_admin: NamedDependency[User],
        organization_service: NamedDependency[OrganizationService],
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[OrganizationResponse]:
        page_limit, page_offset = resolve_page(limit, offset)
        orgs = await organization_service.list(current_admin, limit=page_limit, offset=page_offset)
        return [OrganizationResponse.from_entity(o) for o in orgs]

    @post("/{organization_id:uuid}/teams")
    async def create_team(
        self,
        organization_id: FromPath[UUID],
        data: CreateTeamRequest,
        current_admin: NamedDependency[User],
        team_service: NamedDependency[TeamService],
    ) -> TeamResponse:
        team = await team_service.create_team(
            actor=current_admin,
            organization_id=organization_id,
            name=data.name,
            admin_email=data.admin_email,
        )
        return TeamResponse.from_entity(team)
