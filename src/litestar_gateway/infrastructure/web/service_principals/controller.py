"""Team service principals + their keys.

Administration is JWT-only (a human team admin or platform admin, via
`TeamService.ensure_team_permission`) — deliberately NOT dual-auth: a key must
never be able to create service principals or mint keys, so a leaked key
cannot self-replicate. The keys issued here are the only ones allowed to hold
management scope.
"""

from __future__ import annotations

from uuid import UUID

from litestar import Controller, Request, delete, get, patch, post
from litestar.di import NamedDependency, Provide
from litestar.params import FromPath, FromQuery

from litestar_gateway.application.service_principal_service import ServicePrincipalService
from litestar_gateway.application.team_service import TeamService
from litestar_gateway.domain.authorization import Permission
from litestar_gateway.domain.entities import KeyScope, User
from litestar_gateway.domain.exceptions import InvalidKeyScope
from litestar_gateway.domain.pagination import resolve_page
from litestar_gateway.domain.ports import AuditLog
from litestar_gateway.infrastructure.web.audit.recorder import record_audit
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_user
from litestar_gateway.infrastructure.web.teams.schemas import (
    CreatedKeyResponse,
    CreateKeyRequest,
    CreateServicePrincipalRequest,
    ServicePrincipalResponse,
    SetServicePrincipalEnabledRequest,
)


class ServicePrincipalController(Controller):
    path = "/teams"
    tags = ["service-principals"]
    # JWT only: a human team admin manages service principals.
    dependencies = {"current_user": Provide(provide_current_user)}

    @post("/{team_id:uuid}/service-principals")
    async def create_sp(
        self,
        request: Request,
        team_id: FromPath[UUID],
        data: CreateServicePrincipalRequest,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        service_principal_service: NamedDependency[ServicePrincipalService],
        audit_log: NamedDependency[AuditLog],
    ) -> ServicePrincipalResponse:
        await team_service.ensure_team_permission(
            current_user, team_id, Permission.SERVICE_PRINCIPALS_MANAGE
        )
        sp = await service_principal_service.create(team_id, data.name)
        await record_audit(
            audit_log,
            request,
            current_user,
            "service_principal.create",
            target_type="service_principal",
            target_id=sp.id,
            detail=f"team {team_id}",
        )
        return ServicePrincipalResponse.from_entity(sp)

    @get("/{team_id:uuid}/service-principals")
    async def list_sps(
        self,
        team_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        service_principal_service: NamedDependency[ServicePrincipalService],
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[ServicePrincipalResponse]:
        await team_service.ensure_team_permission(
            current_user, team_id, Permission.SERVICE_PRINCIPALS_MANAGE
        )
        page_limit, page_offset = resolve_page(limit, offset)
        sps = await service_principal_service.list(team_id, limit=page_limit, offset=page_offset)
        return [ServicePrincipalResponse.from_entity(s) for s in sps]

    @patch("/{team_id:uuid}/service-principals/{sp_id:uuid}")
    async def set_sp_enabled(
        self,
        request: Request,
        team_id: FromPath[UUID],
        sp_id: FromPath[UUID],
        data: SetServicePrincipalEnabledRequest,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        service_principal_service: NamedDependency[ServicePrincipalService],
        audit_log: NamedDependency[AuditLog],
    ) -> ServicePrincipalResponse:
        await team_service.ensure_team_permission(
            current_user, team_id, Permission.SERVICE_PRINCIPALS_MANAGE
        )
        sp = await service_principal_service.set_enabled(team_id, sp_id, data.enabled)
        await record_audit(
            audit_log,
            request,
            current_user,
            "service_principal.enable" if data.enabled else "service_principal.disable",
            target_type="service_principal",
            target_id=sp_id,
        )
        return ServicePrincipalResponse.from_entity(sp)

    @delete("/{team_id:uuid}/service-principals/{sp_id:uuid}")
    async def delete_sp(
        self,
        request: Request,
        team_id: FromPath[UUID],
        sp_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        service_principal_service: NamedDependency[ServicePrincipalService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        await team_service.ensure_team_permission(
            current_user, team_id, Permission.SERVICE_PRINCIPALS_MANAGE
        )
        await service_principal_service.delete(team_id, sp_id)
        await record_audit(
            audit_log,
            request,
            current_user,
            "service_principal.delete",
            target_type="service_principal",
            target_id=sp_id,
        )

    @post("/{team_id:uuid}/service-principals/{sp_id:uuid}/keys")
    async def issue_sp_key(
        self,
        request: Request,
        team_id: FromPath[UUID],
        sp_id: FromPath[UUID],
        data: CreateKeyRequest,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        service_principal_service: NamedDependency[ServicePrincipalService],
        audit_log: NamedDependency[AuditLog],
    ) -> CreatedKeyResponse:
        await team_service.ensure_team_permission(
            current_user, team_id, Permission.SERVICE_PRINCIPALS_MANAGE
        )
        try:
            scope = KeyScope(data.scope)
        except ValueError:
            valid = ", ".join(s.value for s in KeyScope)
            raise InvalidKeyScope(f"scope must be one of: {valid}") from None
        issued = await service_principal_service.issue_key(
            team_id=team_id,
            sp_id=sp_id,
            created_by=current_user.id,
            name=data.name,
            scope=scope,
        )
        await record_audit(
            audit_log,
            request,
            current_user,
            "service_principal.key.create",
            target_type="service_principal",
            target_id=sp_id,
            detail=f"key {issued.key.id} scope {scope.value}",
        )
        return CreatedKeyResponse.from_issued(issued)
