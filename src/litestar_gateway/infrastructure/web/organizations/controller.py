"""Organization & team creation — platform-admin only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from litestar import Controller, Request, delete, get, patch, post
from litestar.di import NamedDependency, Provide
from litestar.params import FromPath, FromQuery

from litestar_gateway.application.organization_service import OrganizationService
from litestar_gateway.application.team_service import TeamService
from litestar_gateway.domain.entities import User
from litestar_gateway.domain.pagination import resolve_page
from litestar_gateway.domain.ports import AuditLog
from litestar_gateway.infrastructure.web.audit.recorder import record_audit
from litestar_gateway.infrastructure.web.organizations.schemas import (
    CreateOrganizationRequest,
    OrganizationResponse,
    OrganizationSpendResponse,
    TeamSpendResponse,
    UpdateOrganizationRequest,
)
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_admin
from litestar_gateway.infrastructure.web.teams.schemas import CreateTeamRequest, TeamResponse

# Spend rollup window: default 30 days, clamped to a sane range so a stray query
# value can't ask for an unbounded or negative window.
_DEFAULT_SPEND_DAYS = 30
_MAX_SPEND_DAYS = 365


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
        org = await organization_service.create(
            current_admin, data.name, description=data.description, tags=data.tags
        )
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
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[OrganizationResponse]:
        page_limit, page_offset = resolve_page(limit, offset)
        orgs = await organization_service.list(current_admin, limit=page_limit, offset=page_offset)
        return [OrganizationResponse.from_entity(o) for o in orgs]

    @get("/{organization_id:uuid}")
    async def get_organization(
        self,
        organization_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        organization_service: NamedDependency[OrganizationService],
    ) -> OrganizationResponse:
        org = await organization_service.get(current_admin, organization_id)
        return OrganizationResponse.from_entity(org)

    @get("/{organization_id:uuid}/spend")
    async def organization_spend(
        self,
        organization_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        organization_service: NamedDependency[OrganizationService],
        days: FromQuery[int | None] = None,
    ) -> OrganizationSpendResponse:
        """Recorded cost over the last `days` (default 30, clamped to 1..365),
        summed across the org's teams with a per-team breakdown. Read-only."""
        window = max(1, min(days or _DEFAULT_SPEND_DAYS, _MAX_SPEND_DAYS))
        since = datetime.now(UTC) - timedelta(days=window)
        rollup = await organization_service.spend_rollup(
            current_admin, organization_id, since=since
        )
        return OrganizationSpendResponse(
            organization_id=organization_id,
            since=rollup.since,
            total_cost=rollup.total_cost,
            teams=[
                TeamSpendResponse(team_id=ts.team.id, name=ts.team.name, cost=ts.cost)
                for ts in rollup.teams
            ],
        )

    @patch("/{organization_id:uuid}")
    async def update_organization(
        self,
        request: Request,
        organization_id: FromPath[UUID],
        data: UpdateOrganizationRequest,
        current_admin: NamedDependency[User],
        organization_service: NamedDependency[OrganizationService],
        audit_log: NamedDependency[AuditLog],
    ) -> OrganizationResponse:
        org = await organization_service.update(
            current_admin,
            organization_id,
            data.name,
            description=data.description,
            tags=data.tags,
        )
        await record_audit(
            audit_log,
            request,
            current_admin,
            "organization.update",
            target_type="organization",
            target_id=org.id,
            detail=data.name,
        )
        return OrganizationResponse.from_entity(org)

    @delete("/{organization_id:uuid}")
    async def delete_organization(
        self,
        request: Request,
        organization_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        organization_service: NamedDependency[OrganizationService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        # Refuses with 409 (OrganizationNotEmpty) if the org still has teams —
        # no cascade. Returns the deleted entity so we can audit its name.
        org = await organization_service.delete(current_admin, organization_id)
        await record_audit(
            audit_log,
            request,
            current_admin,
            "organization.delete",
            target_type="organization",
            target_id=org.id,
            detail=org.name,
        )

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
