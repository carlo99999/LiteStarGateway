"""What one API key may see in the MCP registry.

The gateway is a **registry**, not a tool gateway: it never calls an MCP server on
a client's behalf and never proxies `tools/call`. A client asks the registry which
servers it may use, then connects to them itself.

That makes this the whole authorization surface of the feature on the inference
side, so the filter is worth reading carefully. Four things narrow the answer, and
they are deliberately layered rather than collapsed into one query:

1. **team visibility** — own + extended + global, minus what the team detached.
   Through `visible_to`, never `server.team_id == team_id`, which is the spelling
   that silently drops globals and extended servers (D5);
2. **the server's kill switch** — `enabled`, `service_principal.enabled` semantics;
3. **the server's own tool allowlist** — `exposes`, an operator narrowing what a
   server offers regardless of who is asking;
4. **the key's tool policy** — `ApiKeyToolPolicy`, absent meaning unrestricted
   except for `destructive` (D4). This is the only layer that differs between two
   keys of the same team, and it is the reason this is not just `visible_to`.

A server whose every tool the key is refused is **omitted**: "the MCPs this key has
access to" cannot include one it may invoke nothing on. The exception is a server
nobody has run discovery against, which is listed with `discovered=False` — an
empty inventory there means "unknown", not "nothing", and hiding it would tell a
client the server does not exist when in fact nobody has asked it yet.
"""

from __future__ import annotations

import dataclasses
from uuid import UUID

from litestar_gateway.domain.callable_alias import CallableOrigin
from litestar_gateway.domain.mcp import McpServer, McpTool
from litestar_gateway.domain.ports.mcp import ApiKeyToolPolicyRepository, McpServerRepository


@dataclasses.dataclass(frozen=True)
class RegistryEntry:
    """One server as a client may see it.

    `auth` is absent, and not by omission: the token is envelope-encrypted and no
    read path decrypts it, so there is nothing here that could serialize it. A
    client connecting directly needs its own copy — `requires_auth` says whether it
    will need one, which is the most this surface can honestly tell it.
    """

    server: McpServer
    tools: tuple[McpTool, ...]
    # False means discovery never ran, so `tools` is unknown rather than empty.
    discovered: bool

    @property
    def origin(self) -> CallableOrigin:
        return self.server.origin


class McpRegistry:
    """Read-only, and built without a discovery port on purpose: listing the
    registry must never be able to cause outbound traffic. A client refreshing this
    endpoint is not a reason for the gateway to connect to somebody's tool server —
    discovery stays an explicit, `tools:manage` action on the admin surface."""

    def __init__(self, servers: McpServerRepository, policies: ApiKeyToolPolicyRepository) -> None:
        self._servers = servers
        self._policies = policies

    async def for_api_key(self, team_id: UUID, api_key_id: UUID) -> list[RegistryEntry]:
        """No permission check, deliberately.

        The API key *is* the authorization here, exactly as it is for
        `GET /v1/models`: the router's auth middleware has already resolved it to a
        team and refused a key without inference scope. Demanding a team
        `Permission` on top would ask a question about a *user* that a key-only
        caller cannot answer.
        """
        policy = await self._policies.get(api_key_id)
        entries: list[RegistryEntry] = []
        for server in await self._servers.visible_to(team_id):
            if not server.enabled:
                continue
            inventory = await self._servers.tools(server.id)
            discovered = server.last_discovered_at is not None
            permitted = tuple(
                tool
                for tool in inventory
                if server.exposes(tool.name) and self._key_permits(policy, tool)
            )
            # Nothing invocable *and* we know the full inventory ⇒ this key has no
            # access to this server. Without discovery we know nothing, so it stays.
            if not permitted and discovered:
                continue
            entries.append(RegistryEntry(server=server, tools=permitted, discovered=discovered))
        return entries

    @staticmethod
    def _key_permits(policy, tool: McpTool) -> bool:
        """Absent policy is permissive — except for `destructive`, which needs
        explicit per-key enablement (D4). An unclassified tool counts as
        destructive, so this also excludes every tool nobody has reviewed yet."""
        if policy is None:
            return not tool.effect.needs_explicit_key_grant
        return policy.permits(tool.name, tool.effect)
