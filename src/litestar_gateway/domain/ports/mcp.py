"""Port — persistence for MCP tool servers (Plan 20).

Secrets are the repository's business alone, the same asymmetry the guardrail
port uses: every method returns entities with `has_auth` set and the token
withheld, except `auth_token`, which only the call path uses. A management
endpoint literally cannot leak what it never reads.

`visible_to` is the one method that must not be re-implemented by a caller. A
server is visible to a team when it owns it, when it was extended to it, or when
it is global — minus what the team detached. Spelled as `server.team_id ==
team_id` anywhere else, globals and extended servers silently disappear, which is
the shape behind two findings in this codebase already.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from litestar_gateway.domain.mcp import (
    ApiKeyToolPolicy,
    McpServer,
    McpServerGrant,
    McpServerProposal,
    McpTool,
    ProposalStatus,
    ToolEffect,
)


@runtime_checkable
class McpServerRepository(Protocol):
    async def get(self, server_id: UUID) -> McpServer | None:
        """By id, regardless of owner — callers pair it with `visible_to`."""
        ...

    async def visible_to(self, team_id: UUID) -> list[McpServer]:
        """Own + extended + global, minus this team's detaches."""
        ...

    async def list_global(self) -> list[McpServer]:
        """Global servers only — the platform admin's inventory, not a team's."""
        ...

    async def add(self, server: McpServer, *, auth: str | None = None) -> McpServer: ...

    async def stage_add(self, server: McpServer, *, auth: str | None = None) -> object:
        """Insert without committing, for a caller that owns the transaction.

        Returns the persistence row rather than an entity, because the only caller
        needs it to have been written — not to read it back. Approving a proposal
        stages the server, then claims the decision that references it, so the two
        cannot diverge.
        """
        ...

    async def update(self, server: McpServer, *, auth: str | None = None) -> McpServer: ...

    async def remove(self, server_id: UUID) -> bool:
        """Delete the resource. Only ever called for a server the team owns, or
        by a platform admin — see `suppress` for the other case."""
        ...

    async def suppress(self, server_id: UUID, team_id: UUID) -> None:
        """Detach a server this team does not own, reversibly and for it alone."""
        ...

    async def unsuppress(self, server_id: UUID, team_id: UUID) -> bool: ...

    async def grant(self, server_id: UUID, team_id: UUID) -> None: ...

    async def revoke_grant(self, server_id: UUID, team_id: UUID) -> bool: ...

    async def others_named(self, name: str, exclude_id: UUID) -> list[McpServer]:
        """Other servers carrying this name — a server has no alias, so an
        extension or promotion that would duplicate one is refused."""
        ...

    async def list_grants(self, server_id: UUID) -> list[McpServerGrant]: ...

    async def revoke_grant_by_id(self, grant_id: UUID) -> bool: ...

    async def make_global(self, server_id: UUID) -> McpServer | None: ...

    async def auth_token(self, server_id: UUID) -> str | None:
        """The decrypted bearer token. Call path only."""
        ...

    async def tools(self, server_id: UUID) -> list[McpTool]: ...

    async def replace_tools(self, server_id: UUID, tools: list[McpTool]) -> list[McpTool]:
        """Store a fresh `tools/list` result, preserving each tool's declared
        effect: the inventory is a cache, the effect is operator state."""
        ...

    async def set_effect(self, server_id: UUID, tool_name: str, effect: ToolEffect) -> bool: ...


@runtime_checkable
class McpDiscoveryPort(Protocol):
    """Asking a server what it offers.

    A port because the application layer must be able to run without it: a
    service built with no discovery cannot cause egress at all, which is a
    stronger statement than "it happens not to call it".
    """

    async def list_tools(self, server: McpServer, *, auth: str | None = None) -> list[McpTool]:
        """The server's advertised tools, with effects **seeded** from its own
        hints. The caller stores them through `replace_tools`, which keeps
        whatever an operator declared — so a hint can never overwrite a
        classification."""
        ...


@runtime_checkable
class McpServerProposalRepository(Protocol):
    """Proposals, whose one interesting method is `decide`.

    Everything else here is ordinary CRUD; `decide` is the concurrency contract
    the whole slice rests on, and it is the invite's `mark_used` shape for the
    same reason: two admins clicking approve at the same time must produce one
    server, and a read-then-write cannot promise that however carefully it is
    written.
    """

    async def add(
        self, proposal: McpServerProposal, *, auth: str | None = None
    ) -> McpServerProposal: ...

    async def get(self, proposal_id: UUID) -> McpServerProposal | None:
        """By id, regardless of team — callers check the team themselves, so a
        proposal from another tenant is a 404 rather than a cross-team read."""
        ...

    async def list_for_team(
        self, team_id: UUID, *, status: ProposalStatus | None = None
    ) -> list[McpServerProposal]: ...

    async def decide(
        self,
        proposal_id: UUID,
        *,
        status: ProposalStatus,
        decided_by: UUID | None,
        decided_at: datetime,
        reason: str | None = None,
        server_id: UUID | None = None,
    ) -> bool:
        """One conditional `UPDATE ... WHERE status = 'pending'`.

        Returns False when the row was already decided, which is the *only*
        reliable way to know: whatever a caller read a moment earlier may already
        be stale.

        It decides the shared transaction: a granted claim commits it, a refused
        one rolls it back. So a caller may stage the server it is approving first —
        and must, since the claim references it — knowing a lost race takes that
        insert down with it.
        """
        ...

    async def auth_token(self, proposal_id: UUID) -> str | None:
        """The decrypted bearer token, read once at approval to hand it to the
        server being created. Nothing else reads it, and no response carries it."""
        ...


@runtime_checkable
class ApiKeyToolPolicyRepository(Protocol):
    """Per-key tool policy, on the `api_key_budget` precedent: one row per key,
    read on the call path, absent meaning unrestricted."""

    async def get(self, api_key_id: UUID) -> ApiKeyToolPolicy | None: ...

    async def set(self, policy: ApiKeyToolPolicy) -> ApiKeyToolPolicy: ...

    async def remove(self, api_key_id: UUID) -> bool: ...
