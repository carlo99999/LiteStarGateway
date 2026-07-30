"""Team-scoped MCP tool servers (team-admin or platform-admin).

Authorization is not repeated here — `McpServerService` and
`ApiKeyToolPolicyService` demand `tools:read` / `tools:manage` on every call, so a
future entry point inherits the check instead of having to remember it. The same
division the guardrail controller uses.

Two things about this surface are worth knowing before changing it:

**`DELETE` returns a body.** Removing a server the team owns deletes it; removing
a global or extended one detaches it from this team alone. A 204 that means two
things would leave the console and the audit log unable to tell them apart.

**A server another team owns is a 404, not a 403.** A 403 would confirm the
resource exists, which is exactly what a team must not learn about another
tenant's registry.
"""

from __future__ import annotations

from uuid import UUID

from litestar import Controller, Request, delete, get, patch, post, put
from litestar.di import NamedDependency, Provide
from litestar.params import FromPath

from litestar_gateway.application.mcp_service import ApiKeyToolPolicyService, McpServerService
from litestar_gateway.domain.entities import Principal
from litestar_gateway.domain.exceptions import InvalidMcpServer
from litestar_gateway.domain.mcp import ToolEffect
from litestar_gateway.domain.ports import AuditLog
from litestar_gateway.infrastructure.web.audit.recorder import record_audit
from litestar_gateway.infrastructure.web.mcp.schemas import (
    CreateMcpServerRequest,
    DeclareToolEffectRequest,
    KeyToolPolicyResponse,
    McpRemovalResponse,
    McpServerResponse,
    McpToolResponse,
    SetKeyToolPolicyRequest,
    UpdateMcpServerRequest,
)
from litestar_gateway.infrastructure.web.principal import provide_principal


def _effect(value: str) -> ToolEffect:
    try:
        return ToolEffect(value)
    except ValueError as exc:
        allowed = ", ".join(e.value for e in ToolEffect)
        raise InvalidMcpServer(f"effect must be one of: {allowed}") from exc


class McpServerController(Controller):
    path = "/teams"
    tags = ["mcp"]
    dependencies = {"principal": Provide(provide_principal)}

    @get(
        "/{team_id:uuid}/mcp-servers",
        summary="List the MCP servers this team can use",
        description=(
            "The team's own servers, those extended to it, and global ones — minus "
            "any it detached. Bearer tokens are never returned."
        ),
    )
    async def list_servers(
        self,
        team_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        mcp_server_service: NamedDependency[McpServerService],
    ) -> list[McpServerResponse]:
        servers = await mcp_server_service.list_servers(principal, team_id)
        return [McpServerResponse.from_entity(server) for server in servers]

    @get("/{team_id:uuid}/mcp-servers/{server_id:uuid}", summary="Get one MCP server")
    async def get_server(
        self,
        team_id: FromPath[UUID],
        server_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        mcp_server_service: NamedDependency[McpServerService],
    ) -> McpServerResponse:
        server = await mcp_server_service.get_server(principal, team_id, server_id)
        return McpServerResponse.from_entity(server)

    @get(
        "/{team_id:uuid}/mcp-servers/{server_id:uuid}/tools",
        summary="The server's discovered tool inventory",
        description=(
            "What the server last advertised, with the effect an operator declared "
            "for each tool. A tool nobody classified counts as `destructive`."
        ),
    )
    async def list_tools(
        self,
        team_id: FromPath[UUID],
        server_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        mcp_server_service: NamedDependency[McpServerService],
    ) -> list[McpToolResponse]:
        tools = await mcp_server_service.list_tools(principal, team_id, server_id)
        return [McpToolResponse.from_entity(tool) for tool in tools]

    @post(
        "/{team_id:uuid}/mcp-servers",
        summary="Register an MCP server for this team",
        description=(
            "The url must be https and must resolve into `MCP_ALLOWED_HOSTS`, "
            "re-checked on every call rather than only here."
        ),
    )
    async def create_server(
        self,
        request: Request,
        team_id: FromPath[UUID],
        data: CreateMcpServerRequest,
        principal: NamedDependency[Principal],
        mcp_server_service: NamedDependency[McpServerService],
        audit_log: NamedDependency[AuditLog],
    ) -> McpServerResponse:
        server = await mcp_server_service.create_server(
            principal,
            team_id,
            name=data.name,
            url=data.url,
            auth=data.auth,
            tool_allowlist=tuple(data.tool_allowlist or ()),
            enabled=True if data.enabled is None else data.enabled,
        )
        await record_audit(
            audit_log,
            request,
            principal.user,
            "mcp_server.create",
            target_type="mcp_server",
            target_id=server.id,
            # The endpoint, deliberately: which host the gateway was authorized to
            # reach is the fact an audit reader needs. The token is not here to be
            # leaked — no entity this layer sees carries it.
            detail=f"'{server.name}' → {server.url}",
        )
        return McpServerResponse.from_entity(server)

    @patch("/{team_id:uuid}/mcp-servers/{server_id:uuid}", summary="Update the team's MCP server")
    async def update_server(
        self,
        request: Request,
        team_id: FromPath[UUID],
        server_id: FromPath[UUID],
        data: UpdateMcpServerRequest,
        principal: NamedDependency[Principal],
        mcp_server_service: NamedDependency[McpServerService],
        audit_log: NamedDependency[AuditLog],
    ) -> McpServerResponse:
        server = await mcp_server_service.update_server(
            principal,
            team_id,
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
            principal.user,
            "mcp_server.update",
            target_type="mcp_server",
            target_id=server.id,
            detail=f"'{server.name}' enabled={server.enabled}",
        )
        return McpServerResponse.from_entity(server)

    @delete(
        "/{team_id:uuid}/mcp-servers/{server_id:uuid}",
        summary="Remove the team's server, or detach a shared one",
        description=(
            "Deletes a server this team owns. A global or extended server is "
            "**detached** instead — hidden from this team alone and left live for "
            "every other one. The response says which happened."
        ),
        status_code=200,
    )
    async def remove_server(
        self,
        request: Request,
        team_id: FromPath[UUID],
        server_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        mcp_server_service: NamedDependency[McpServerService],
        audit_log: NamedDependency[AuditLog],
    ) -> McpRemovalResponse:
        outcome = await mcp_server_service.remove_server(principal, team_id, server_id)
        # Two audit actions, not one with a detail: an operator searching for who
        # deleted a server should not have to read details to exclude detaches.
        await record_audit(
            audit_log,
            request,
            principal.user,
            f"mcp_server.{outcome}",
            target_type="mcp_server",
            target_id=server_id,
            detail=f"team {team_id}",
        )
        return McpRemovalResponse(outcome=outcome)

    @post(
        "/{team_id:uuid}/mcp-servers/{server_id:uuid}/reattach",
        summary="Undo a detach",
        description=(
            "Takes back a shared server this team previously detached, without "
            "needing a platform admin — which is what makes the detach reversible."
        ),
        status_code=200,
    )
    async def reattach_server(
        self,
        request: Request,
        team_id: FromPath[UUID],
        server_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        mcp_server_service: NamedDependency[McpServerService],
        audit_log: NamedDependency[AuditLog],
    ) -> McpServerResponse:
        server = await mcp_server_service.reattach_server(principal, team_id, server_id)
        await record_audit(
            audit_log,
            request,
            principal.user,
            "mcp_server.reattach",
            target_type="mcp_server",
            target_id=server_id,
            detail=f"team {team_id}",
        )
        return McpServerResponse.from_entity(server)

    @put(
        "/{team_id:uuid}/mcp-servers/{server_id:uuid}/tools/{tool_name:str}/effect",
        summary="Declare what a tool does",
        description=(
            "`read`, `write`, or `destructive`. Declared by an operator, never "
            "detected from the server's own annotations. Only the owning team may "
            "set it: a team that merely sees a shared server must not be able to "
            "relabel a destructive tool as harmless for everybody else."
        ),
        status_code=204,
    )
    async def declare_effect(
        self,
        request: Request,
        team_id: FromPath[UUID],
        server_id: FromPath[UUID],
        tool_name: FromPath[str],
        data: DeclareToolEffectRequest,
        principal: NamedDependency[Principal],
        mcp_server_service: NamedDependency[McpServerService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        effect = _effect(data.effect)
        await mcp_server_service.declare_effect(principal, team_id, server_id, tool_name, effect)
        # Audited because widening a tool's classification is the change someone
        # will later need explained.
        await record_audit(
            audit_log,
            request,
            principal.user,
            "mcp_tool.declare_effect",
            target_type="mcp_server",
            target_id=server_id,
            detail=f"{tool_name} = {effect.value}",
        )

    # ── per-key tool policy (design §2.5) ────────────────────────────────────
    #
    # Under `tools:read`/`tools:manage` on both sides, not `keys:issue`. Round
    # 15's ISSUE-042 was that exact asymmetry on per-key spend caps: the write
    # landed in one permission domain and the read in another, so an issuer could
    # write an object and then be refused reading it back.

    @get(
        "/{team_id:uuid}/keys/{key_id:uuid}/tool-policy",
        summary="Which tools one API key may invoke",
        description=(
            "`restricted: false` is the default and not an error: a key with no "
            "policy may call every tool except those declared `destructive`."
        ),
    )
    async def get_key_tool_policy(
        self,
        team_id: FromPath[UUID],
        key_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        api_key_tool_policy_service: NamedDependency[ApiKeyToolPolicyService],
    ) -> KeyToolPolicyResponse:
        policy = await api_key_tool_policy_service.get_policy(principal, team_id, key_id)
        if policy is None:
            return KeyToolPolicyResponse.unrestricted(key_id)
        return KeyToolPolicyResponse.from_entity(policy)

    @put(
        "/{team_id:uuid}/keys/{key_id:uuid}/tool-policy",
        summary="Create or replace an API key's tool policy",
    )
    async def set_key_tool_policy(
        self,
        request: Request,
        team_id: FromPath[UUID],
        key_id: FromPath[UUID],
        data: SetKeyToolPolicyRequest,
        principal: NamedDependency[Principal],
        api_key_tool_policy_service: NamedDependency[ApiKeyToolPolicyService],
        audit_log: NamedDependency[AuditLog],
    ) -> KeyToolPolicyResponse:
        policy = await api_key_tool_policy_service.set_policy(
            principal,
            team_id,
            key_id,
            allowed_tools=tuple(data.allowed_tools or ()),
            destructive_enabled=bool(data.destructive_enabled),
        )
        await record_audit(
            audit_log,
            request,
            principal.user,
            "api_key.tool_policy.set",
            target_type="api_key",
            target_id=key_id,
            detail=(
                f"{len(policy.allowed_tools) or 'all'} tool(s), "
                f"destructive={policy.destructive_enabled}"
            ),
        )
        return KeyToolPolicyResponse.from_entity(policy)

    @delete(
        "/{team_id:uuid}/keys/{key_id:uuid}/tool-policy",
        summary="Remove an API key's tool policy",
        description="The key becomes permissive again, so this is a widening.",
        status_code=204,
    )
    async def delete_key_tool_policy(
        self,
        request: Request,
        team_id: FromPath[UUID],
        key_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        api_key_tool_policy_service: NamedDependency[ApiKeyToolPolicyService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        removed = await api_key_tool_policy_service.remove_policy(principal, team_id, key_id)
        # Idempotent rather than a 404: the caller asked for "no policy", and a
        # key that already had none is in exactly that state.
        await record_audit(
            audit_log,
            request,
            principal.user,
            "api_key.tool_policy.delete",
            target_type="api_key",
            target_id=key_id,
            detail="removed" if removed else "no policy was set",
        )
