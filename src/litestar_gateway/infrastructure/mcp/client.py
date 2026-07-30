"""Asking a registered MCP server what tools it offers (Plan 20 S3).

**This is a handshake, not one POST.** The plan's line for this slice reads
"`tools/list` over HTTP", which understates the transport. Streamable HTTP
requires the client to:

1. POST `initialize`, with `Accept` listing **both** `application/json` and
   `text/event-stream`, and `MCP-Protocol-Version`;
2. carry the `Mcp-Session-Id` from that response — if the server returned one — on
   every subsequent request. A server that keeps session state answers 400 to a
   request that omits it;
3. POST `notifications/initialized` (202, no body);
4. POST `tools/list`, whose response may be **either** a JSON object or an SSE
   stream. The spec says a client MUST support both.

A client that skips the handshake works against stateless servers and fails
against stateful ones, which is a defect that only shows up in production against
half the real implementations.

**Egress is re-resolved here, per discovery.** The allowlist is checked when a
server is registered *and* again now, because a name that resolved into the
allowlisted range at save time can resolve elsewhere later — ISSUE-034 exactly.
The whole handshake is pinned to the addresses resolved once at its start, so the
session cannot be handed to a different host halfway through.

One thing an operator has to know about that re-check: it constrains *addresses*
only when the allowlist entry is an address or a CIDR. A **name** entry authorizes
the hostname and deliberately leaves its resolution unconstrained — the operator
is vouching for that name's DNS (`domain/egress_policy.py` says so explicitly).
So `MCP_ALLOWED_HOSTS=10.9.0.0/24:8443` refuses a rebind and
`MCP_ALLOWED_HOSTS=tools.internal:8443` does not. Re-resolving still matters for
the second form — it is what makes removing an entry take effect on existing
servers — but it is not a rebinding defence there.

**Effects are seeded, never trusted.** MCP `annotations` are hints written by the
server about itself. They give a *new* tool its initial classification, and an
unclassified tool counts as `destructive`. They never overwrite what an operator
declared — `replace_tools` is what enforces that, and it is why seeding is safe
to do on every refresh.
"""

from __future__ import annotations

import ipaddress
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx

from litestar_gateway.application.egress import resolve_optionally_allowlisted_addresses
from litestar_gateway.application.routing.webhook import post_to_approved_address
from litestar_gateway.domain.egress_policy import EgressAllowlist
from litestar_gateway.domain.exceptions import McpDiscoveryFailed
from litestar_gateway.domain.mcp import McpServer, McpTool, ToolEffect

# The protocol revision this client speaks. Sent on every request: a server that
# receives no `MCP-Protocol-Version` is told by the spec to assume 2025-03-26,
# and guessing on our behalf is not something to rely on.
PROTOCOL_VERSION = "2025-06-18"

# Discovery is an operator action behind a management endpoint, not something on
# the request path, so this can be more generous than the guardrail webhook's
# 2 s — but it is still a hard ceiling. Three sequential requests share it.
DEFAULT_TIMEOUT_MS = 5000

CLIENT_INFO = {"name": "litestar-gateway", "version": "1"}

_JSON_CONTENT = "application/json"
_SSE_CONTENT = "text/event-stream"
_ACCEPT = f"{_JSON_CONTENT}, {_SSE_CONTENT}"

# A tool whose schema is absurd is refused rather than stored: this inventory
# becomes a declaration in a model request, and `domain.chat_tool_policy` applies
# the real per-provider limits there. This bound only stops one server filling
# the table.
MAX_TOOLS = 256


class McpDiscoveryClient:
    """One-shot `tools/list` against one server, over a fresh session."""

    def __init__(
        self,
        *,
        allowlist: EgressAllowlist | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        client_factory: Any = None,
    ) -> None:
        self._allowlist = allowlist or EgressAllowlist(entries=())
        self._timeout_seconds = timeout_ms / 1000
        self._client_factory = client_factory or (
            lambda seconds: httpx.AsyncClient(timeout=seconds)
        )

    async def list_tools(self, server: McpServer, *, auth: str | None = None) -> list[McpTool]:
        url = httpx.URL(server.url)
        host = url.host
        if not host:  # pragma: no cover - refused at registration
            raise McpDiscoveryFailed(f"server {server.name!r} has no host in its url")
        try:
            addresses = await resolve_optionally_allowlisted_addresses(
                host, url.port, self._allowlist
            )
        except ValueError as exc:
            # The ISSUE-034 case: reachable when registered, not now. Refusing here
            # is the whole point of re-resolving. Which check refused depends on
            # whether an allowlist is configured — with none, the deny-list did,
            # meaning the name now resolves somewhere private.
            refused_by = (
                "the SSRF deny-list (no MCP_ALLOWED_HOSTS is configured)"
                if self._allowlist.is_empty
                else "MCP_ALLOWED_HOSTS"
            )
            raise McpDiscoveryFailed(
                f"server {server.name!r} at {host} is no longer permitted by {refused_by}: {exc}"
            ) from exc
        except OSError as exc:
            raise McpDiscoveryFailed(f"server {server.name!r} at {host} did not resolve") from exc

        base_headers = {
            "Host": url.netloc.decode("ascii"),
            "Content-Type": _JSON_CONTENT,
            "Accept": _ACCEPT,
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if auth:
            base_headers["Authorization"] = f"Bearer {auth}"

        async with self._client_factory(self._timeout_seconds) as client:
            session = await self._initialize(client, server, url, host, addresses, base_headers)
            headers = dict(base_headers)
            if session:
                headers["Mcp-Session-Id"] = session
            await self._send_initialized(client, server, url, host, addresses, headers)
            payload = await self._request(
                client, server, url, host, addresses, headers, "tools/list"
            )
        return self._parse_tools(server, payload)

    # ── the handshake ────────────────────────────────────────────────────────

    async def _initialize(
        self,
        client: httpx.AsyncClient,
        server: McpServer,
        url: httpx.URL,
        host: str,
        addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...],
        headers: dict[str, str],
    ) -> str | None:
        """Returns the session id when the server issues one.

        `None` is a normal answer, not a failure: a stateless server issues no
        session, and sending an empty `Mcp-Session-Id` would be worse than
        sending none.
        """
        response = await self._post(
            client,
            server,
            url,
            host,
            addresses,
            headers,
            {
                "jsonrpc": "2.0",
                "id": str(uuid4()),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    # Honest and minimal: this gateway lists and calls tools. It
                    # offers the server no roots and no sampling, so claiming
                    # either would invite requests it cannot answer.
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            },
        )
        self._require_ok(server, response, "initialize")
        self._result_of(server, self._decode(server, response, "initialize"), "initialize")
        return response.headers.get("mcp-session-id")

    async def _send_initialized(
        self,
        client: httpx.AsyncClient,
        server: McpServer,
        url: httpx.URL,
        host: str,
        addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...],
        headers: dict[str, str],
    ) -> None:
        response = await self._post(
            client,
            server,
            url,
            host,
            addresses,
            headers,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        # The spec says 202 with no body. Accept any 2xx: a server answering 200
        # to a notification is out of spec in a way that harms nobody, and
        # failing the discovery over it would be pedantry with a cost.
        if response.status_code >= 400:
            raise McpDiscoveryFailed(
                f"server {server.name!r} refused the initialized notification: "
                f"HTTP {response.status_code}"
            )

    async def _request(
        self,
        client: httpx.AsyncClient,
        server: McpServer,
        url: httpx.URL,
        host: str,
        addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...],
        headers: dict[str, str],
        method: str,
    ) -> dict[str, Any]:
        response = await self._post(
            client,
            server,
            url,
            host,
            addresses,
            headers,
            {"jsonrpc": "2.0", "id": str(uuid4()), "method": method},
        )
        self._require_ok(server, response, method)
        return self._result_of(server, self._decode(server, response, method), method)

    async def _post(
        self,
        client: httpx.AsyncClient,
        server: McpServer,
        url: httpx.URL,
        host: str,
        addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...],
        headers: dict[str, str],
        body: dict[str, Any],
    ) -> httpx.Response:
        """Every request goes through the shared pinned-IP primitive: connection
        to a validated address, `Host` and SNI kept as the hostname, redirects
        off. A redirect is how a permitted target hands the session to one that
        was never checked."""
        try:
            return await post_to_approved_address(
                client,
                url,
                host,
                addresses,
                None,
                headers,
                content=json.dumps(body, separators=(",", ":")).encode(),
            )
        except httpx.HTTPError as exc:
            raise McpDiscoveryFailed(
                f"server {server.name!r} at {host} is unreachable: {exc}"
            ) from exc

    # ── parsing ──────────────────────────────────────────────────────────────

    def _require_ok(self, server: McpServer, response: httpx.Response, method: str) -> None:
        if response.status_code == 404:
            # A session the server has since dropped. The spec's remedy is to
            # start a new session — which the *next* discovery does, since every
            # one of them initializes from scratch.
            raise McpDiscoveryFailed(
                f"server {server.name!r} dropped the session during {method}; retry discovery"
            )
        if response.status_code >= 400:
            raise McpDiscoveryFailed(
                f"server {server.name!r} answered HTTP {response.status_code} to {method}"
            )

    def _decode(self, server: McpServer, response: httpx.Response, method: str) -> Any:
        """A JSON-RPC response arrives as a JSON body or inside an SSE stream,
        and the spec requires a client to handle both."""
        content_type = response.headers.get("content-type", "")
        if content_type.startswith(_SSE_CONTENT):
            return self._decode_sse(server, response.text, method)
        try:
            return response.json()
        except ValueError as exc:
            raise McpDiscoveryFailed(
                f"server {server.name!r} answered {method} with a body that is not JSON"
            ) from exc

    def _decode_sse(self, server: McpServer, text: str, method: str) -> Any:
        """The last `data:` frame that parses as a JSON-RPC response for us.

        A server may send notifications ahead of the response, so the first frame
        is not necessarily the answer.
        """
        found: Any = None
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                frame = json.loads(line[len("data:") :].strip())
            except ValueError:
                continue
            if isinstance(frame, dict) and ("result" in frame or "error" in frame):
                found = frame
        if found is None:
            raise McpDiscoveryFailed(f"server {server.name!r} streamed no response to {method}")
        return found

    def _result_of(self, server: McpServer, body: Any, method: str) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise McpDiscoveryFailed(f"server {server.name!r} answered {method} with a non-object")
        if "error" in body:
            error = body["error"]
            detail = error.get("message") if isinstance(error, dict) else None
            raise McpDiscoveryFailed(
                f"server {server.name!r} refused {method}: {detail or 'unspecified error'}"
            )
        result = body.get("result")
        if not isinstance(result, dict):
            raise McpDiscoveryFailed(f"server {server.name!r} answered {method} without a result")
        return result

    def _parse_tools(self, server: McpServer, result: dict[str, Any]) -> list[McpTool]:
        """Strict: an off-contract inventory is no inventory.

        A partially-parsed list would show an operator a shorter set of tools with
        nothing saying that entries were dropped, which is worse than an error
        they can act on.
        """
        raw = result.get("tools")
        if not isinstance(raw, list):
            raise McpDiscoveryFailed(f"server {server.name!r} returned no 'tools' array")
        if len(raw) > MAX_TOOLS:
            raise McpDiscoveryFailed(
                f"server {server.name!r} advertised {len(raw)} tools, over the {MAX_TOOLS} limit"
            )
        now = datetime.now(UTC)
        tools: list[McpTool] = []
        seen: set[str] = set()
        for entry in raw:
            if not isinstance(entry, dict):
                raise McpDiscoveryFailed(f"server {server.name!r} returned a non-object tool")
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise McpDiscoveryFailed(f"server {server.name!r} returned a tool without a name")
            if name in seen:
                # Two tools under one name make "which one did we call" unanswerable.
                raise McpDiscoveryFailed(f"server {server.name!r} advertised {name!r} twice")
            seen.add(name)
            schema = entry.get("inputSchema")
            if schema is not None and not isinstance(schema, dict):
                raise McpDiscoveryFailed(
                    f"server {server.name!r} gave tool {name!r} a non-object inputSchema"
                )
            description = entry.get("description")
            if description is not None and not isinstance(description, str):
                raise McpDiscoveryFailed(
                    f"server {server.name!r} gave tool {name!r} a non-string description"
                )
            if not server.exposes(name):
                # The operator's per-server allowlist, applied at the boundary so
                # a tool nobody wants never reaches the inventory at all.
                continue
            tools.append(
                McpTool(
                    id=uuid4(),
                    server_id=server.id,
                    name=name,
                    description=description or "",
                    schema=dict(schema or {}),
                    effect=seed_effect(entry.get("annotations")),
                    discovered_at=now,
                )
            )
        return tools


def seed_effect(annotations: Any) -> ToolEffect:
    """The *initial* classification for a tool nobody has classified yet.

    Only ever a seed. `replace_tools` keeps whatever an operator declared, so a
    server cannot downgrade a tool it previously advertised as destructive by
    changing its own hints later — which is the whole content of "declared, never
    detected".

    Absent or unreadable hints mean `destructive`: the safe end, and the same
    default the domain type carries.
    """
    if not isinstance(annotations, dict):
        return ToolEffect.DESTRUCTIVE
    if annotations.get("readOnlyHint") is True:
        return ToolEffect.READ
    if annotations.get("destructiveHint") is False:
        return ToolEffect.WRITE
    return ToolEffect.DESTRUCTIVE
