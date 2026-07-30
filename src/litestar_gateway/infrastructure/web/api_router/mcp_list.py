"""`GET /v1/mcp/servers` — the MCP registry, as one API key sees it.

The sibling of `GET /v1/models`, and authenticated the same way: the router's auth
middleware resolves the key to a team, `request.user` is the team id, and the key
itself is the authorization — no team `Permission` is demanded, because a key-only
caller has no user to answer that question about.

**The gateway is a registry, not a tool gateway.** It tells a client which MCP
servers it may use and which tools each one offers; the client connects to them
itself. Nothing here contacts a tool server — listing the registry must never be a
way to make the gateway emit outbound traffic, so discovery stays an explicit
`tools:manage` action on the admin surface.

The bearer token is never returned. It is envelope-encrypted and no read path
decrypts it, so there is nothing in this response that could leak it; a client
connecting directly needs its own copy, and `requires_auth` is the most this surface
can honestly tell it.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from litestar import Request, get
from litestar.di import NamedDependency

from litestar_gateway.application.mcp_registry import McpRegistry, RegistryEntry

_OBJECT = "mcp_server"


def _entry(entry: RegistryEntry) -> dict[str, Any]:
    server = entry.server
    return {
        # The name, not the uuid: a server is referenced by name everywhere in this
        # feature (there is no alias), and uniqueness is enforced so it stays
        # unambiguous for whoever reads this list.
        "id": server.name,
        "object": _OBJECT,
        "url": server.url,
        # own | extended | global — where it came from, the same vocabulary the
        # models and routing surfaces use.
        "origin": server.origin.value,
        # Whether the client will need a bearer token of its own. The gateway has
        # one on file if this is true, but never hands it over.
        "requires_auth": server.has_auth,
        # False ⇒ nobody has asked this server what it offers, so `tools` is
        # unknown rather than empty. Collapsing the two would show a working server
        # as one with no tools.
        "discovered": entry.discovered,
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                # read | write | destructive, declared by an operator and never
                # detected from what the server says about itself.
                "effect": tool.effect.value,
                "input_schema": dict(tool.schema),
            }
            for tool in entry.tools
        ],
    }


@get(
    "/v1/mcp/servers",
    summary="The MCP servers this API key may use, with their tools",
    description=(
        "A **registry**: the gateway does not execute tools and does not proxy MCP "
        "traffic. It lists the servers this key may use — its team's own, those "
        "extended to it, and global ones, minus any the team detached — each with "
        "the tools this key is permitted to invoke.\n\n"
        "A tool declared `destructive` appears only for a key whose policy enables "
        "them explicitly, and an unclassified tool counts as destructive. A server "
        "this key may invoke nothing on is omitted; one nobody has run discovery "
        "against is listed with `discovered: false` and an empty `tools`, because "
        "there the inventory is unknown rather than empty.\n\n"
        "Bearer tokens are never returned — `requires_auth` says whether the server "
        "needs one."
    ),
)
async def list_mcp_servers(
    request: Request,
    mcp_registry: NamedDependency[McpRegistry],
) -> dict[str, Any]:
    team_id = UUID(request.user)
    entries = await mcp_registry.for_api_key(team_id, request.auth.id)
    return {"object": "list", "data": [_entry(entry) for entry in entries]}
