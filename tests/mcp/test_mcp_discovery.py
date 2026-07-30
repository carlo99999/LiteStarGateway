"""Plan 20 S3 — the discovery client: the handshake, both response shapes, and
the re-resolution that makes the allowlist mean something at call time.

The headline test is `test_a_server_whose_dns_left_the_allowlist_is_refused_on_the
_second_discovery`. Round 15's ISSUE-034 was exactly this on the provider path: a
host validated when a credential was saved, never re-checked when it was used, so
a name that later resolved elsewhere was called anyway. The plan asked for that
regression to exist for this surface from day one instead of being found by a
review, so it is written here against a resolver that moves between two calls.

The transport is a real `httpx.AsyncClient` over `MockTransport`, not a stubbed
`post`: the headers, the pinned-IP rewrite and the SSE parsing are the parts most
likely to be wrong, and a fake that answers `client.post` would exercise none of
them.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from uuid import uuid4

import httpx
import pytest

from litestar_gateway.domain.egress_policy import parse_allowlist
from litestar_gateway.domain.exceptions import McpDiscoveryFailed
from litestar_gateway.domain.mcp import McpServer, ToolEffect
from litestar_gateway.infrastructure.mcp.client import (
    PROTOCOL_VERSION,
    McpDiscoveryClient,
    seed_effect,
)

ALLOWLIST = parse_allowlist(("tools.internal:8443",))
SESSION = "1868a90c-abcd"


def _server(name: str = "github", *, tool_allowlist: tuple[str, ...] = ()) -> McpServer:
    return McpServer(
        id=uuid4(),
        team_id=uuid4(),
        name=name,
        url="https://tools.internal:8443/mcp",
        enabled=True,
        created_at=None,  # type: ignore[arg-type]
        tool_allowlist=tool_allowlist,
    )


@pytest.fixture(autouse=True)
def resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tools.internal` does not resolve on a laptop, and the allowlist resolves
    before matching (which is what makes a CIDR entry meaningful). Patching the
    resolver is the established pattern in `tests/egress/`."""
    import litestar_gateway.application.egress as egress_module

    async def resolve(host: str) -> list[str]:
        return ["10.9.0.7"]

    monkeypatch.setattr(egress_module, "_resolve_host_addresses", resolve)


def _tools_result(tools: list[dict]) -> dict:
    return {"tools": tools}


def _client(handler: Callable[[httpx.Request], httpx.Response], **kwargs) -> McpDiscoveryClient:
    return McpDiscoveryClient(
        allowlist=ALLOWLIST,
        client_factory=lambda seconds: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=seconds
        ),
        **kwargs,
    )


def _scripted(
    *,
    session: str | None = SESSION,
    tools: list[dict] | None = None,
    sse: bool = False,
    record: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """A conforming server: initialize → 202 for the notification → tools/list."""
    payload = _tools_result(tools if tools is not None else [{"name": "search"}])

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        body = json.loads(request.content)
        method = body.get("method")
        if method == "initialize":
            headers = {"Mcp-Session-Id": session} if session else {}
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}},
                },
                headers=headers,
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            response = {"jsonrpc": "2.0", "id": body["id"], "result": payload}
            if sse:
                return httpx.Response(
                    200,
                    text=f"event: message\ndata: {json.dumps(response)}\n\n",
                    headers={"Content-Type": "text/event-stream"},
                )
            return httpx.Response(200, json=response)
        raise AssertionError(f"unexpected method {method!r}")

    return handler


# ── the handshake ────────────────────────────────────────────────────────────


async def test_the_handshake_runs_in_order_and_carries_the_session(
    tmp_path,
) -> None:
    """A client that POSTs `tools/list` alone works against a stateless server and
    gets a 400 from a stateful one. The order and the session id are the contract."""
    seen: list[httpx.Request] = []

    tools = await _client(_scripted(record=seen)).list_tools(_server())

    assert [json.loads(r.content).get("method") for r in seen] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    # The session the server issued is echoed on everything after initialize...
    assert "mcp-session-id" not in {k.lower() for k in seen[0].headers}
    assert seen[1].headers["mcp-session-id"] == SESSION
    assert seen[2].headers["mcp-session-id"] == SESSION
    assert [tool.name for tool in tools] == ["search"]


async def test_every_request_declares_both_response_shapes_and_the_protocol(
    tmp_path,
) -> None:
    seen: list[httpx.Request] = []

    await _client(_scripted(record=seen)).list_tools(_server())

    for request in seen:
        # The spec requires both to be listed; a server may answer with either.
        assert "application/json" in request.headers["accept"]
        assert "text/event-stream" in request.headers["accept"]
        assert request.headers["mcp-protocol-version"] == PROTOCOL_VERSION
        # Host and SNI keep the hostname while the connection goes to the pinned
        # IP — the shared primitive's guarantee, asserted here because this is a
        # new caller of it.
        assert request.headers["host"] == "tools.internal:8443"
        assert request.url.host == "10.9.0.7"


async def test_a_stateless_server_sends_no_session_and_none_is_invented(
    tmp_path,
) -> None:
    """`None` is a normal answer. An empty `Mcp-Session-Id` would be worse than
    no header at all."""
    seen: list[httpx.Request] = []

    tools = await _client(_scripted(session=None, record=seen)).list_tools(_server())

    for request in seen:
        assert "mcp-session-id" not in {k.lower() for k in request.headers}
    assert [tool.name for tool in tools] == ["search"]


async def test_the_bearer_token_is_sent_and_is_the_only_place_it_appears(
    tmp_path,
) -> None:
    seen: list[httpx.Request] = []

    await _client(_scripted(record=seen)).list_tools(_server(), auth="pw-must-not-leak")

    for request in seen:
        assert request.headers["authorization"] == "Bearer pw-must-not-leak"
        # ...and never in the body, where a log of the payload would keep it.
        assert b"pw-must-not-leak" not in request.content


# ── both response shapes ─────────────────────────────────────────────────────


async def test_a_tools_list_answered_as_an_sse_stream_is_parsed(tmp_path) -> None:
    tools = await _client(_scripted(sse=True)).list_tools(_server())

    assert [tool.name for tool in tools] == ["search"]


async def test_notifications_before_the_response_do_not_confuse_the_parser(
    tmp_path,
) -> None:
    """A server MAY send requests and notifications ahead of the response, so the
    first frame is not necessarily the answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        frames = [
            {"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}},
            {"jsonrpc": "2.0", "id": body["id"], "result": _tools_result([{"name": "search"}])},
        ]
        text = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
        return httpx.Response(200, text=text, headers={"Content-Type": "text/event-stream"})

    tools = await _client(handler).list_tools(_server())

    assert [tool.name for tool in tools] == ["search"]


async def test_a_stream_that_never_carries_a_response_is_an_error(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            text='data: {"jsonrpc":"2.0","method":"notifications/message"}\n\n',
            headers={"Content-Type": "text/event-stream"},
        )

    with pytest.raises(McpDiscoveryFailed, match="streamed no response"):
        await _client(handler).list_tools(_server())


# ── the ISSUE-034 regression ─────────────────────────────────────────────────


async def test_a_server_whose_dns_left_the_allowlist_is_refused_on_the_second_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The finding this slice was told to pre-empt.

    The address is re-resolved on every discovery, so a name that resolved into
    the allowlisted range when the server was registered and resolves elsewhere
    now is refused — and refused *before* any connection is attempted, which is
    what makes it a control rather than a log line.

    **The allowlist entry has to be an address or CIDR for this to bite.** A
    *name* entry authorizes the hostname and deliberately does not constrain what
    it resolves to (`domain/egress_policy.py` states this: the operator is
    vouching for that name and its DNS). So an operator who wants rebinding
    refused has to say `10.9.0.0/24:8443`, not `tools.internal:8443` — which is a
    documentation obligation, not something this client can decide.
    """
    import litestar_gateway.application.egress as egress_module

    inside = parse_allowlist(("10.9.0.0/24:8443",))
    addresses = ["10.9.0.7"]

    async def resolve(host: str) -> list[str]:
        return addresses

    monkeypatch.setattr(egress_module, "_resolve_host_addresses", resolve)
    attempts: list[httpx.Request] = []
    client = McpDiscoveryClient(
        allowlist=inside,
        client_factory=lambda seconds: httpx.AsyncClient(
            transport=httpx.MockTransport(_scripted(record=attempts)), timeout=seconds
        ),
    )
    server = _server()

    first = await client.list_tools(server)
    assert [tool.name for tool in first] == ["search"]
    before = len(attempts)

    # The name now points somewhere else entirely — a rebind, a failover, a typo
    # in somebody's DNS. Nothing about the stored server changed.
    addresses[:] = ["203.0.113.9"]

    with pytest.raises(McpDiscoveryFailed, match="no longer permitted by MCP_ALLOWED_HOSTS"):
        await client.list_tools(server)

    # And no request went out on the second attempt.
    assert len(attempts) == before


async def test_a_host_that_stops_resolving_is_a_typed_failure_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litestar_gateway.application.egress as egress_module

    async def unresolvable(host: str) -> list[str]:
        raise OSError("nodename nor servname provided")

    monkeypatch.setattr(egress_module, "_resolve_host_addresses", unresolvable)

    with pytest.raises(McpDiscoveryFailed, match="did not resolve"):
        await _client(_scripted()).list_tools(_server())


async def test_with_no_allowlist_an_internal_target_is_refused_by_the_deny_list() -> None:
    """`MCP_ALLOWED_HOSTS` is optional, so an empty one no longer refuses
    everything — it falls through to the SSRF deny-list.

    `_server()` points at `tools.internal`, which resolves privately, so it is
    still refused here. What changed is *which* check refuses it and what the fix
    is: an allowlist entry, because reaching a private target is the one thing the
    deny-list never permits on its own. The message has to say so, or an operator
    reads "not permitted" and goes looking for a permission.
    """
    client = McpDiscoveryClient(
        client_factory=lambda seconds: httpx.AsyncClient(
            transport=httpx.MockTransport(_scripted()), timeout=seconds
        )
    )

    with pytest.raises(McpDiscoveryFailed, match="deny-list") as refused:
        await client.list_tools(_server())

    assert "MCP_ALLOWED_HOSTS" in str(refused.value)


async def test_with_no_allowlist_a_public_target_is_discovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the optional default, and the reason it changed: a hosted
    MCP server is usable on a deployment where nobody configured anything.

    Asserted on the discovery path specifically. Registration and discovery apply
    the rule separately — the second one re-resolves per call — so a change that
    only loosened registration would leave the feature registering servers it then
    refused to query.
    """
    import litestar_gateway.application.egress as egress_module

    async def public(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(egress_module, "_resolve_host_addresses", public)
    client = McpDiscoveryClient(
        client_factory=lambda seconds: httpx.AsyncClient(
            transport=httpx.MockTransport(_scripted()), timeout=seconds
        )
    )

    tools = await client.list_tools(_server())

    assert [tool.name for tool in tools] == ["search"]


# ── strict parsing: an off-contract inventory is no inventory ────────────────


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({}, "no 'tools' array"),
        ({"tools": "search"}, "no 'tools' array"),
        ({"tools": ["search"]}, "non-object tool"),
        ({"tools": [{"description": "no name"}]}, "without a name"),
        ({"tools": [{"name": ""}]}, "without a name"),
        ({"tools": [{"name": "a"}, {"name": "a"}]}, "twice"),
        ({"tools": [{"name": "a", "inputSchema": "nope"}]}, "non-object inputSchema"),
        ({"tools": [{"name": "a", "description": 7}]}, "non-string description"),
    ],
)
async def test_a_malformed_inventory_is_a_typed_error_never_a_partial_one(
    result: dict, message: str
) -> None:
    """A partially-parsed list would show an operator a shorter set of tools with
    nothing saying entries were dropped — worse than an error they can act on."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    with pytest.raises(McpDiscoveryFailed, match=message):
        await _client(handler).list_tools(_server())


async def test_a_jsonrpc_error_carries_the_servers_own_message(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32601, "message": "tools are disabled here"},
            },
        )

    with pytest.raises(McpDiscoveryFailed, match="tools are disabled here"):
        await _client(handler).list_tools(_server())


async def test_a_dropped_session_says_so_rather_than_failing_opaquely(tmp_path) -> None:
    """The spec's remedy for a 404 is a fresh session, which the next discovery
    is — every one of them initializes from scratch."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
                headers={"Mcp-Session-Id": SESSION},
            )
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(404)

    with pytest.raises(McpDiscoveryFailed, match="dropped the session"):
        await _client(handler).list_tools(_server())


async def test_a_body_that_is_not_json_is_refused(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not an mcp server</html>")

    with pytest.raises(McpDiscoveryFailed, match="not JSON"):
        await _client(handler).list_tools(_server())


async def test_an_unreachable_server_is_a_typed_failure(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(McpDiscoveryFailed, match="unreachable"):
        await _client(handler).list_tools(_server())


async def test_an_absurd_number_of_tools_is_refused(tmp_path) -> None:
    many = [{"name": f"tool_{i}"} for i in range(257)]

    with pytest.raises(McpDiscoveryFailed, match="over the 256 limit"):
        await _client(_scripted(tools=many)).list_tools(_server())


# ── effects are seeded, never trusted ────────────────────────────────────────


@pytest.mark.parametrize(
    ("annotations", "expected"),
    [
        ({"readOnlyHint": True}, ToolEffect.READ),
        ({"destructiveHint": False}, ToolEffect.WRITE),
        ({"destructiveHint": True}, ToolEffect.DESTRUCTIVE),
        ({}, ToolEffect.DESTRUCTIVE),
        (None, ToolEffect.DESTRUCTIVE),
        ("read-only, promise", ToolEffect.DESTRUCTIVE),
        ({"readOnlyHint": "true"}, ToolEffect.DESTRUCTIVE),
    ],
)
def test_an_unclassified_or_unreadable_hint_seeds_destructive(
    annotations: object, expected: ToolEffect
) -> None:
    """The safe end. A string `"true"` is not `True`: a hint we cannot read is a
    hint we do not act on."""
    assert seed_effect(annotations) is expected


async def test_the_seeded_effect_reaches_the_inventory(tmp_path) -> None:
    tools = await _client(
        _scripted(
            tools=[
                {"name": "search", "annotations": {"readOnlyHint": True}},
                {"name": "write_file", "annotations": {"destructiveHint": False}},
                {"name": "delete_repo"},
            ]
        )
    ).list_tools(_server())

    assert {tool.name: tool.effect for tool in tools} == {
        "search": ToolEffect.READ,
        "write_file": ToolEffect.WRITE,
        "delete_repo": ToolEffect.DESTRUCTIVE,
    }


async def test_the_per_server_allowlist_keeps_unwanted_tools_out_of_the_inventory(
    tmp_path,
) -> None:
    """Applied at the boundary: a tool nobody wants never gets stored, so it
    cannot be enabled later by editing a policy."""
    tools = await _client(
        _scripted(tools=[{"name": "search"}, {"name": "delete_repo"}])
    ).list_tools(_server(tool_allowlist=("search",)))

    assert [tool.name for tool in tools] == ["search"]
