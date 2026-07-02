"""Organization & team creation — platform-admin only."""

from __future__ import annotations

from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.di import NamedDependency, Provide
from litestar.params import FromPath

from litestar_test.application.organization_service import OrganizationService
from litestar_test.application.team_service import TeamService
from litestar_test.domain.entities import User
from litestar_test.domain.ports import AuditLog
from litestar_test.infrastructure.web.audit.recorder import record_audit
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
        request: Request,
        data: CreateOrganizationRequest,
        current_admin: NamedDependency[User],
        organization_service: NamedDependency[OrganizationService],
        audit_log: NamedDependency[AuditLog],
    ) -> OrganizationResponse:
        org = await organization_service.create(current_admin, data.name)
        await record_audit(
            audit_log,
            request,
            current_admin,
            "organization.create",
            target_type="organization",
            target_id=org.id,
            detail=data.name,
        )
        return OrganizationResponse.from_entity(org)

    @get()
    async def list_organizations(
        self,
        current_admin: NamedDependency[User],
        organization_service: NamedDependency[OrganizationService],
    ) -> list[OrganizationResponse]:
        orgs = await organization_service.list(current_admin)
        return [OrganizationResponse.from_entity(o) for o in orgs]

    @post("/{organization_id:uuid}/teams")
    async def create_team(
        self,
        request: Request,
        organization_id: FromPath[UUID],
        data: CreateTeamRequest,
        current_admin: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        audit_log: NamedDependency[AuditLog],
    ) -> TeamResponse:
        team = await team_service.create_team(
            actor=current_admin,
            organization_id=organization_id,
            name=data.name,
            admin_email=data.admin_email,
        )
        await record_audit(
            audit_log,
            request,
            current_admin,
            "team.create",
            target_type="team",
            target_id=team.id,
            detail=f"{data.name} in organization {organization_id}",
        )
        return TeamResponse.from_entity(team)
