"""DTOs for MCP tool servers.

No response type carries the bearer token — only `has_auth`. As with guardrail
rules, that is not politeness: the repository never hands the value to this layer,
so there is nothing here that could serialize it by accident.

Every new optional flag is `bool | None = None` rather than `bool = False`.
Litestar puts a field with a plain default in the OpenAPI `required` list, which
breaks the generated TypeScript client — measured while shipping Round 15.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from litestar.params import Parameter

from litestar_gateway.domain.mcp import ApiKeyToolPolicy, McpServer, McpServerGrant, McpTool


@dataclass(frozen=True)
class CreateMcpServerRequest:
    """Register a remote MCP server for this team."""

    name: Annotated[str, Parameter(description="Operator-facing name, unique among its owner's.")]
    url: Annotated[
        str,
        Parameter(
            description=(
                "The server's https endpoint. Must resolve into `MCP_ALLOWED_HOSTS` "
                "— the platform's one veto over a team-owned server — and must not "
                "carry userinfo."
            )
        ),
    ]
    auth: Annotated[
        str | None,
        Parameter(
            description=(
                "Bearer token this gateway presents to the server. Stored "
                "envelope-encrypted; never returned by any endpoint."
            )
        ),
    ] = None
    tool_allowlist: Annotated[
        list[str] | None,
        Parameter(
            description=(
                "Restrict which advertised tools this server exposes. Omitted or "
                "empty exposes everything it advertises; a non-empty list is "
                "exhaustive."
            )
        ),
    ] = None
    enabled: Annotated[bool | None, Parameter(description="Whether the server is usable.")] = None


@dataclass(frozen=True)
class UpdateMcpServerRequest:
    """Partial update. Omitted fields are unchanged — including `auth`, which
    cannot be read back and so cannot be resubmitted."""

    name: Annotated[str | None, Parameter(description="New name.")] = None
    url: Annotated[str | None, Parameter(description="New https endpoint; re-validated.")] = None
    auth: Annotated[
        str | None, Parameter(description="Rotate the bearer token. Omit to keep the current one.")
    ] = None
    tool_allowlist: Annotated[
        list[str] | None, Parameter(description="Replaces the whole allowlist.")
    ] = None
    enabled: Annotated[bool | None, Parameter(description="Enable or disable the server.")] = None


@dataclass(frozen=True)
class DeclareToolEffectRequest:
    """Effects are declared by an operator, never detected from what the server
    says about itself."""

    effect: Annotated[str, Parameter(description="`read`, `write`, or `destructive`.")]


@dataclass(frozen=True)
class ExtendMcpServerRequest:
    team_ids: Annotated[list[UUID], Parameter(description="Teams the server becomes visible to.")]


@dataclass(frozen=True)
class SetKeyToolPolicyRequest:
    """Create or replace one key's tool policy."""

    allowed_tools: Annotated[
        list[str] | None,
        Parameter(
            description=(
                "Tools this key may invoke. Omitted or empty means unrestricted — "
                "the row exists mainly to carry `destructive_enabled`, so enabling "
                "destructive tools does not require enumerating every read tool."
            )
        ),
    ] = None
    destructive_enabled: Annotated[
        bool | None,
        Parameter(
            description=(
                "Permit tools declared `destructive` (they delete or irreversibly "
                "change something). Off unless set: an unclassified tool counts as "
                "destructive, so this is the one class the permissive default "
                "excludes."
            )
        ),
    ] = None


@dataclass(frozen=True)
class McpServerResponse:
    id: UUID
    team_id: UUID | None
    name: str
    url: str
    enabled: bool
    has_auth: bool
    origin: str  # own | extended | global
    tool_allowlist: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    # `null` means discovery never ran. With an empty tool list that is a
    # different state from "it ran and this server offers nothing", and a console
    # that cannot tell them apart shows a working server as unconfigured.
    last_discovered_at: datetime | None = None

    @classmethod
    def from_entity(cls, server: McpServer) -> McpServerResponse:
        return cls(
            id=server.id,
            team_id=server.team_id,
            name=server.name,
            url=server.url,
            enabled=server.enabled,
            has_auth=server.has_auth,
            origin=server.origin.value,
            tool_allowlist=list(server.tool_allowlist),
            created_at=server.created_at,
            last_discovered_at=server.last_discovered_at,
        )


@dataclass(frozen=True)
class McpToolResponse:
    name: str
    description: str
    effect: str
    schema: dict[str, Any] = field(default_factory=dict)
    discovered_at: datetime | None = None

    @classmethod
    def from_entity(cls, tool: McpTool) -> McpToolResponse:
        return cls(
            name=tool.name,
            description=tool.description,
            effect=tool.effect.value,
            schema=dict(tool.schema),
            discovered_at=tool.discovered_at,
        )


@dataclass(frozen=True)
class McpRemovalResponse:
    """What `DELETE` actually did.

    Not a 204: removing a server the team owns deletes it, while removing a
    global or extended one detaches it for this team alone. One status code
    meaning two things is a trap for whoever reads the console or the audit log.
    """

    outcome: Annotated[
        str,
        Parameter(
            description=(
                "`deleted` (the team's own server is gone) or `detached` (a shared "
                "server is hidden from this team and still live for the others)."
            )
        ),
    ]


@dataclass(frozen=True)
class McpServerGrantResponse:
    """A server extended to a team."""

    id: UUID
    server_id: UUID
    team_id: UUID
    created_at: datetime | None = None

    @classmethod
    def from_entity(cls, grant: McpServerGrant) -> McpServerGrantResponse:
        return cls(
            id=grant.id,
            server_id=grant.server_id,
            team_id=grant.team_id,
            created_at=grant.created_at,
        )


@dataclass(frozen=True)
class KeyToolPolicyResponse:
    """One key's tool policy. `restricted` is false when no row exists, which is
    the default state rather than an error."""

    api_key_id: UUID
    restricted: bool
    destructive_enabled: bool
    # No default on purpose. A dataclass default keeps the field out of the
    # OpenAPI `required` list, so the generated client types it as possibly
    # `undefined` and every consumer has to handle a state the gateway never
    # sends — the mirror of Round 15's lesson, where a request flag with a plain
    # default landed *in* `required` and broke the client the other way.
    allowed_tools: list[str]
    created_at: datetime | None = None

    @classmethod
    def unrestricted(cls, api_key_id: UUID) -> KeyToolPolicyResponse:
        return cls(
            api_key_id=api_key_id,
            restricted=False,
            destructive_enabled=False,
            allowed_tools=[],
        )

    @classmethod
    def from_entity(cls, policy: ApiKeyToolPolicy) -> KeyToolPolicyResponse:
        return cls(
            api_key_id=policy.api_key_id,
            restricted=True,
            destructive_enabled=policy.destructive_enabled,
            allowed_tools=list(policy.allowed_tools),
            created_at=policy.created_at,
        )
