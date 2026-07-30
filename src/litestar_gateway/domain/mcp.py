"""MCP tool servers: the domain types (Plan 20 S0).

An MCP server is a **team** resource — its admins create and remove it — while
the platform keeps one veto: `MCP_ALLOWED_HOSTS` bounds where any of them may
point. That is the opposite of a `Credential`, deliberately: a credential is
platform-owned because it carries *the platform's* secret, and an MCP server
carries the team's own.

Visibility reuses `CallableOrigin` rather than inventing an enum, so a server can
be the team's own, extended to chosen teams, or global — exactly the three
`Model` already has. The resolution of "which servers can this team see" is
deliberately **not** here: it belongs to one function in the application layer,
because a check spelled `server.team_id == team_id` silently excludes globals and
extended servers, and that spelling has already produced two findings in this
codebase (guardrail scope on global models, and Round 12's ISSUE-020).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from litestar_gateway.domain.callable_alias import CallableOrigin


class ToolEffect(StrEnum):
    """What invoking a tool does, as declared by an operator.

    Declared, never detected — the rule Plan 18 established for model
    capabilities, for the same reason: a value inferred from what a server says
    about itself is a value the server controls. An MCP `annotations` block may
    seed this, but it never decides it, and a tool nobody classified counts as
    `DESTRUCTIVE` until someone says otherwise.
    """

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"

    @property
    def needs_explicit_key_grant(self) -> bool:
        """`destructive` is the one class a permissive default does not cover.

        Absent per-key policy means unrestricted, the polarity a missing spend
        cap already has — so the feature works the moment a server is
        registered. Destructive tools are carved out of that default: a key
        issued for a low-trust application must not be able to delete something
        because a prompt asked it to.
        """
        return self is ToolEffect.DESTRUCTIVE


@dataclasses.dataclass(frozen=True)
class McpServer:
    """A registered remote MCP server.

    `team_id` is `None` for a global server, matching how a global `Model` is
    spelled. `auth` never appears here: the bearer token lives encrypted in the
    persistence layer and is decrypted only on the call path, so no entity a
    response is built from can carry it — the same shape as a guardrail rule's
    signing secret.
    """

    id: UUID
    team_id: UUID | None
    name: str
    url: str
    enabled: bool
    created_at: datetime
    has_auth: bool = False
    tool_allowlist: tuple[str, ...] = ()
    origin: CallableOrigin = CallableOrigin.OWN

    @property
    def is_global(self) -> bool:
        return self.team_id is None

    def exposes(self, tool_name: str) -> bool:
        """An empty allowlist exposes everything the server advertises; a
        non-empty one is exhaustive."""
        return not self.tool_allowlist or tool_name in self.tool_allowlist


@dataclasses.dataclass(frozen=True)
class McpTool:
    """One tool discovered from a server, with the effect an operator declared.

    `schema` is the JSON Schema the server advertised for its arguments. It is
    stored as discovered and validated by `domain.chat_tool_policy` at the point
    it becomes a declaration in a model request — the existing limits (count,
    schema bytes, depth, per-provider name rules) apply unchanged rather than
    being re-implemented here.
    """

    id: UUID
    server_id: UUID
    name: str
    description: str
    schema: dict
    effect: ToolEffect = ToolEffect.DESTRUCTIVE
    discovered_at: datetime | None = None


@dataclasses.dataclass(frozen=True)
class ApiKeyToolPolicy:
    """Per-key restriction, on the `api_key_budget` precedent: a policy row read
    on the call path.

    Absent means unrestricted. Present with an empty `allowed_tools` also means
    unrestricted — the row exists to carry `destructive_enabled`, and an operator
    who only wants to enable destructive tools should not have to enumerate every
    read tool to keep them working.
    """

    id: UUID
    api_key_id: UUID
    team_id: UUID
    allowed_tools: tuple[str, ...] = ()
    destructive_enabled: bool = False
    created_at: datetime | None = None

    def permits(self, tool_name: str, effect: ToolEffect) -> bool:
        if effect.needs_explicit_key_grant and not self.destructive_enabled:
            return False
        return not self.allowed_tools or tool_name in self.allowed_tools
