"""Platform-admin router management: global routers + extension grants.

Mirrors the model platform surface. A global router (`team_id is None`) is
callable by every team and routes in the *caller's* context. Extending shares
the same router (a grant, not a copy). All routes require a platform admin.
"""

from __future__ import annotations

from uuid import UUID

from litestar import Controller, Request, delete, get, post, put
from litestar.di import NamedDependency, Provide
from litestar.exceptions import ClientException
from litestar.params import FromPath

from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.application.team_service import TeamService
from litestar_gateway.domain.entities import User
from litestar_gateway.domain.ports import AuditLog
from litestar_gateway.infrastructure.web.audit.recorder import record_audit
from litestar_gateway.infrastructure.web.routing.controller import (
    ExtendRouterRequest,
    RouterGrantResponse,
    RouterRequest,
    RouterResponse,
)
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_admin


class PlatformRouterController(Controller):
    path = "/platform/routers"
    tags = ["routers"]
    dependencies = {"current_admin": Provide(provide_current_admin)}

    @post("", summary="Create a global (platform) router callable by every team")
    async def create_global(
        self,
        request: Request,
        data: RouterRequest,
        current_admin: NamedDependency[User],
        router_service: NamedDependency[RouterService],
        audit_log: NamedDependency[AuditLog],
    ) -> RouterResponse:
        router = await router_service.create(data.to_entity(None))
        await record_audit(
            audit_log,
            request,
            current_admin,
            "router.create_global",
            target_type="router",
            target_id=router.id,
            detail=router.name,
        )
        return RouterResponse.from_entity(router)

    @get("", summary="List global (platform) routers")
    async def list_global(
        self,
        current_admin: NamedDependency[User],
        router_service: NamedDependency[RouterService],
    ) -> list[RouterResponse]:
        return [RouterResponse.from_entity(r) for r in await router_service.list_global()]

    @put("/{router_id:uuid}", summary="Replace a global router definition")
    async def update_global(
        self,
        request: Request,
        router_id: FromPath[UUID],
        data: RouterRequest,
        current_admin: NamedDependency[User],
        router_service: NamedDependency[RouterService],
        audit_log: NamedDependency[AuditLog],
    ) -> RouterResponse:
        existing = await router_service.get_any(router_id)
        router = await router_service.update_global(
            data.to_entity(None, router_id, origin_team_id=existing.origin_team_id)
        )
        await record_audit(
            audit_log,
            request,
            current_admin,
            "router.update_global",
            target_type="router",
            target_id=router_id,
            detail="global",
        )
        return RouterResponse.from_entity(router)

    @delete("/{router_id:uuid}", summary="Delete a global router")
    async def delete_global(
        self,
        request: Request,
        router_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        router_service: NamedDependency[RouterService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        await router_service.delete_global(router_id)
        await record_audit(
            audit_log,
            request,
            current_admin,
            "router.delete_global",
            target_type="router",
            target_id=router_id,
            detail="global",
        )

    @post("/{router_id:uuid}/make-global", summary="Promote a team router to global")
    async def make_global(
        self,
        request: Request,
        router_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        router_service: NamedDependency[RouterService],
        audit_log: NamedDependency[AuditLog],
    ) -> RouterResponse:
        router = await router_service.make_global(router_id)
        await record_audit(
            audit_log,
            request,
            current_admin,
            "router.make_global",
            target_type="router",
            target_id=router_id,
            detail=router.name,
        )
        return RouterResponse.from_entity(router)

    @post("/{router_id:uuid}/extend", summary="Extend a team router to other teams")
    async def extend(
        self,
        request: Request,
        router_id: FromPath[UUID],
        data: ExtendRouterRequest,
        current_admin: NamedDependency[User],
        router_service: NamedDependency[RouterService],
        team_service: NamedDependency[TeamService],
        audit_log: NamedDependency[AuditLog],
    ) -> list[RouterGrantResponse]:
        router = await router_service.get_any(router_id)
        if router.team_id is None:
            raise ClientException("A global router is already available to every team")
        source_team = await team_service.get_team(current_admin, router.team_id)
        grants = await router_service.extend(router_id, source_team.name, data.team_ids)
        await record_audit(
            audit_log,
            request,
            current_admin,
            "router.extend",
            target_type="router",
            target_id=router_id,
            detail=f"{router.name} → {len(grants)} team(s)",
        )
        return [RouterGrantResponse.from_entity(g) for g in grants]

    @get("/{router_id:uuid}/grants", summary="Teams a router is extended to")
    async def list_grants(
        self,
        router_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        router_service: NamedDependency[RouterService],
    ) -> list[RouterGrantResponse]:
        grants = await router_service.list_grants(router_id)
        return [RouterGrantResponse.from_entity(g) for g in grants]

    @delete("/grants/{grant_id:uuid}", summary="Un-extend (revoke a router grant)")
    async def unextend(
        self,
        request: Request,
        grant_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        router_service: NamedDependency[RouterService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        await router_service.unextend(grant_id)
        await record_audit(
            audit_log,
            request,
            current_admin,
            "router.unextend",
            target_type="router_grant",
            target_id=grant_id,
            detail="revoked",
        )
