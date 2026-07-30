"""Platform-admin MCP servers: global ones + extension grants.

The mirror of `PlatformModelController`, and for the same reasons. A global server
(`team_id is None`) is visible to every team, present and future. "Extending" a
team-owned server shares the *same* server with other teams — a grant, not a copy,
so the source stays the single source of truth for its endpoint and token. Every
route here requires a platform admin.

Two asymmetries against the team surface are deliberate:

- **this `DELETE` really deletes**, including a global server every team is using.
  The team-facing one detaches instead. That split is what stops a team admin
  revoking a capability from every other tenant.
- **`MCP_ALLOWED_HOSTS` still applies.** A platform admin is exempt from the team
  permission check, not from the allowlist: it bounds where the *gateway process*
  may connect, which is a deployment fact rather than a tenancy one.
"""

from __future__ import annotations

from uuid import UUID

from litestar import Controller, Request, delete, get, patch, post
from litestar.di import NamedDependency, Provide
from litestar.params import FromPath

from litestar_gateway.application.mcp_service import McpServerService
from litestar_gateway.domain.entities import User
from litestar_gateway.domain.ports import AuditLog
from litestar_gateway.infrastructure.web.audit.recorder import record_audit
from litestar_gateway.infrastructure.web.mcp.schemas import (
    CreateMcpServerRequest,
    ExtendMcpServerRequest,
    McpServerGrantResponse,
    McpServerResponse,
    UpdateMcpServerRequest,
)
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_admin


class PlatformMcpServerController(Controller):
    path = "/platform/mcp-servers"
    tags = ["mcp"]
    dependencies = {"current_admin": Provide(provide_current_admin)}

    @get("", summary="List global (platform) MCP servers")
    async def list_global(
        self,
        current_admin: NamedDependency[User],
        mcp_server_service: NamedDependency[McpServerService],
    ) -> list[McpServerResponse]:
        servers = await mcp_server_service.list_global_servers()
        return [McpServerResponse.from_entity(server) for server in servers]

    @post("", summary="Register a global MCP server usable by every team")
    async def create_global(
        self,
        request: Request,
        data: CreateMcpServerRequest,
        current_admin: NamedDependency[User],
        mcp_server_service: NamedDependency[McpServerService],
        audit_log: NamedDependency[AuditLog],
    ) -> McpServerResponse:
        server = await mcp_server_service.create_global_server(
            name=data.name,
            url=data.url,
            auth=data.auth,
            tool_allowlist=tuple(data.tool_allowlist or ()),
            enabled=True if data.enabled is None else data.enabled,
        )
        await record_audit(
            audit_log,
            request,
            current_admin,
            "mcp_server.create_global",
            target_type="mcp_server",
            target_id=server.id,
            detail=f"'{server.name}' → {server.url}",
        )
        return McpServerResponse.from_entity(server)

    @patch("/{server_id:uuid}", summary="Update any MCP server")
    async def update_any(
        self,
        request: Request,
        server_id: FromPath[UUID],
        data: UpdateMcpServerRequest,
        current_admin: NamedDependency[User],
        mcp_server_service: NamedDependency[McpServerService],
        audit_log: NamedDependency[AuditLog],
    ) -> McpServerResponse:
        server = await mcp_server_service.update_any_server(
            server_id,
            name=data.name,
            url=data.url,
            auth=data.auth,
            tool_allowlist=(None if data.tool_allowlist is None else tuple(data.tool_allowlist)),
            enabled=data.enabled,
        )
        await record_audit(
            audit_log,
            request,
            current_admin,
            "mcp_server.update_any",
            target_type="mcp_server",
            target_id=server_id,
            detail=f"'{server.name}' enabled={server.enabled}",
        )
        return McpServerResponse.from_entity(server)

    @delete("/{server_id:uuid}", summary="Delete any MCP server, including a global one")
    async def delete_any(
        self,
        request: Request,
        server_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        mcp_server_service: NamedDependency[McpServerService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        await mcp_server_service.delete_any_server(server_id)
        await record_audit(
            audit_log,
            request,
            current_admin,
            "mcp_server.delete_any",
            target_type="mcp_server",
            target_id=server_id,
            detail="platform delete",
        )

    @post("/{server_id:uuid}/make-global", summary="Promote a team server to global")
    async def make_global(
        self,
        request: Request,
        server_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        mcp_server_service: NamedDependency[McpServerService],
        audit_log: NamedDependency[AuditLog],
    ) -> McpServerResponse:
        server = await mcp_server_service.make_global(server_id)
        await record_audit(
            audit_log,
            request,
            current_admin,
            "mcp_server.make_global",
            target_type="mcp_server",
            target_id=server_id,
            detail=server.name,
        )
        return McpServerResponse.from_entity(server)

    @post("/{server_id:uuid}/extend", summary="Extend a team server to other teams")
    async def extend(
        self,
        request: Request,
        server_id: FromPath[UUID],
        data: ExtendMcpServerRequest,
        current_admin: NamedDependency[User],
        mcp_server_service: NamedDependency[McpServerService],
        audit_log: NamedDependency[AuditLog],
    ) -> list[McpServerGrantResponse]:
        grants = await mcp_server_service.extend(server_id, tuple(data.team_ids))
        await record_audit(
            audit_log,
            request,
            current_admin,
            "mcp_server.extend",
            target_type="mcp_server",
            target_id=server_id,
            detail=f"{len(grants)} team(s)",
        )
        return [McpServerGrantResponse.from_entity(grant) for grant in grants]

    @get("/{server_id:uuid}/grants", summary="Teams a server is extended to")
    async def list_grants(
        self,
        server_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        mcp_server_service: NamedDependency[McpServerService],
    ) -> list[McpServerGrantResponse]:
        grants = await mcp_server_service.list_grants(server_id)
        return [McpServerGrantResponse.from_entity(grant) for grant in grants]

    @delete("/grants/{grant_id:uuid}", summary="Un-extend (revoke a grant)", status_code=204)
    async def unextend(
        self,
        request: Request,
        grant_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        mcp_server_service: NamedDependency[McpServerService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        await mcp_server_service.revoke_grant(grant_id)
        await record_audit(
            audit_log,
            request,
            current_admin,
            "mcp_server.unextend",
            target_type="mcp_server_grant",
            target_id=grant_id,
            detail="revoked",
        )
