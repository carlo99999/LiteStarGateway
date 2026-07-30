"""`GET /v1/mcp/servers` — the registry as one API key sees it.

The gateway is a registry: it does not execute tools and does not proxy MCP
traffic. So this endpoint *is* the authorization surface on the inference side, and
the property that matters is the one a single-key test cannot show — **two keys in
the same team see different registries**. Everything else here is a way that
filtering can silently fail open.

Two more shapes are pinned because they have bitten this project before:

- **listing never causes egress.** Discovery is an explicit `tools:manage` action;
  a client polling this endpoint must not turn into traffic to somebody's tool
  server. Asserted with a discovery double that fails the test if called.
- **"no tools" and "never asked" stay distinct.** An empty inventory means two
  different things, and the console needed a migration to tell them apart (S4).
  The client-facing surface must not collapse them either.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_201_CREATED, HTTP_401_UNAUTHORIZED
from litestar.testing import AsyncTestClient
from rbac.conftest import (  # type: ignore[import-not-found]
    ADMIN_EMAIL,
    MASTER_KEY,
    _admin,
    _bearer,
    _team,
)

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings
from litestar_gateway.domain.mcp import McpServer, McpTool, ToolEffect

SERVER_URL = "https://localhost:9443/mcp"


class Discoveries:
    def __init__(self) -> None:
        self.urls: list[str] = []


@pytest.fixture
def discoveries(monkeypatch: pytest.MonkeyPatch) -> Discoveries:
    """Answers a fixed inventory, and records every call so a test can assert there
    were none."""
    from litestar_gateway.infrastructure.mcp.client import McpDiscoveryClient

    recorder = Discoveries()

    async def list_tools(
        self: McpDiscoveryClient, server: McpServer, *, auth: str | None = None
    ) -> list[McpTool]:
        recorder.urls.append(server.url)
        return [
            McpTool(
                id=uuid4(),
                server_id=server.id,
                name="search",
                description="find things",
                schema={"type": "object"},
                effect=ToolEffect.READ,
            ),
            McpTool(
                id=uuid4(),
                server_id=server.id,
                name="delete_repo",
                description="remove a repository",
                schema={"type": "object"},
                # Seeded destructive, which is also the default for anything
                # unclassified — the class the permissive per-key default excludes.
                effect=ToolEffect.DESTRUCTIVE,
            ),
        ]

    monkeypatch.setattr(McpDiscoveryClient, "list_tools", list_tools)
    return recorder


@pytest.fixture
async def client(database_url: str) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=database_url,
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
        mcp_allowed_hosts=("localhost:9443",),
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


async def _key(client: AsyncTestClient, admin: str, team: str, name: str) -> tuple[str, str]:
    """Mint an inference key. Returns (id, plaintext)."""
    minted = await client.post(f"/teams/{team}/keys", json={"name": name}, headers=_bearer(admin))
    assert minted.status_code == HTTP_201_CREATED, minted.text
    body = minted.json()
    # `plaintext` is returned once and never again — the value a client authenticates
    # with, as distinct from `id`, which is what a policy is written against.
    return body["id"], body["plaintext"]


async def _server(
    client: AsyncTestClient, admin: str, team: str, name: str, *, url: str = SERVER_URL
) -> str:
    created = await client.post(
        f"/teams/{team}/mcp-servers", json={"name": name, "url": url}, headers=_bearer(admin)
    )
    assert created.status_code == HTTP_201_CREATED, created.text
    return created.json()["id"]


async def _discover(client: AsyncTestClient, admin: str, team: str, server: str) -> None:
    done = await client.post(
        f"/teams/{team}/mcp-servers/{server}/discover?force=true", headers=_bearer(admin)
    )
    assert done.status_code == HTTP_200_OK, done.text


async def _registry(client: AsyncTestClient, key: str) -> list[dict]:
    listed = await client.get("/v1/mcp/servers", headers=_bearer(key))
    assert listed.status_code == HTTP_200_OK, listed.text
    assert listed.json()["object"] == "list"
    return listed.json()["data"]


# ── the property a single key cannot show ────────────────────────────────────


async def test_two_keys_in_one_team_see_different_registries(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    """The per-key policy is the only layer that differs between two keys of the
    same team, so it is the only thing making this endpoint more than `visible_to`.

    The permissive key sees the read tool and *not* the destructive one; the enabled
    key sees both; the narrowed key sees only what its allowlist names.
    """
    admin = await _admin(client)
    team = await _team(client, admin)
    server = await _server(client, admin, team, "github")
    await _discover(client, admin, team, server)

    plain_id, plain = await _key(client, admin, team, "plain")
    trusted_id, trusted = await _key(client, admin, team, "trusted")
    narrow_id, narrow = await _key(client, admin, team, "narrow")
    await client.put(
        f"/teams/{team}/keys/{trusted_id}/tool-policy",
        json={"destructive_enabled": True},
        headers=_bearer(admin),
    )
    await client.put(
        f"/teams/{team}/keys/{narrow_id}/tool-policy",
        json={"allowed_tools": ["search"]},
        headers=_bearer(admin),
    )

    def names(data: list[dict]) -> list[str]:
        return [tool["name"] for entry in data for tool in entry["tools"]]

    # Absent policy: permissive, except destructive (D4).
    assert names(await _registry(client, plain)) == ["search"]
    # Explicitly enabled: both.
    assert sorted(names(await _registry(client, trusted))) == ["delete_repo", "search"]
    # Narrowed by name, and destructive still excluded because the row did not
    # enable it — the two filters are independent.
    assert names(await _registry(client, narrow)) == ["search"]
    assert plain_id != trusted_id


async def test_a_server_this_key_may_invoke_nothing_on_is_omitted(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    """A key that may invoke nothing on a server does not "have access to" it, so
    listing it would send a client to a server it can do nothing with."""
    admin = await _admin(client)
    team = await _team(client, admin)
    server = await _server(client, admin, team, "github")
    await _discover(client, admin, team, server)
    key_id, key = await _key(client, admin, team, "narrow")
    # An allowlist naming a tool this server does not advertise: nothing left.
    await client.put(
        f"/teams/{team}/keys/{key_id}/tool-policy",
        json={"allowed_tools": ["nonexistent_tool"]},
        headers=_bearer(admin),
    )

    assert await _registry(client, key) == []


async def test_the_servers_own_allowlist_narrows_every_key(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    """An operator narrowing what a server exposes applies regardless of who asks —
    a different layer from the per-key policy, and it must not be skippable by a key
    with a permissive one."""
    admin = await _admin(client)
    team = await _team(client, admin)
    created = await client.post(
        f"/teams/{team}/mcp-servers",
        json={"name": "github", "url": SERVER_URL, "tool_allowlist": ["search"]},
        headers=_bearer(admin),
    )
    assert created.status_code == HTTP_201_CREATED, created.text
    await _discover(client, admin, team, created.json()["id"])
    key_id, key = await _key(client, admin, team, "trusted")
    await client.put(
        f"/teams/{team}/keys/{key_id}/tool-policy",
        json={"destructive_enabled": True},
        headers=_bearer(admin),
    )

    data = await _registry(client, key)

    # `destructive_enabled` cannot resurrect a tool the server does not expose.
    assert [tool["name"] for entry in data for tool in entry["tools"]] == ["search"]


# ── the two empty states, kept apart ─────────────────────────────────────────


async def test_a_server_nobody_discovered_is_listed_as_undiscovered(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    """An empty inventory means "unknown" here, not "nothing".

    Omitting it — the same rule the previous test applies — would tell a client the
    server does not exist, when in fact nobody has asked it yet. This is the S4
    distinction (`last_discovered_at`) reaching the client-facing surface.
    """
    admin = await _admin(client)
    team = await _team(client, admin)
    await _server(client, admin, team, "github")
    _, key = await _key(client, admin, team, "plain")

    data = await _registry(client, key)

    assert [(e["id"], e["discovered"], e["tools"]) for e in data] == [("github", False, [])]
    # And nothing was contacted to find that out.
    assert discoveries.urls == []


async def test_listing_the_registry_never_contacts_a_tool_server(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    """A client polling this endpoint must not become outbound traffic. Discovery is
    an explicit `tools:manage` action, and the registry is built without a discovery
    port at all — which is stronger than promising it does not call one."""
    admin = await _admin(client)
    team = await _team(client, admin)
    server = await _server(client, admin, team, "github")
    await _discover(client, admin, team, server)
    before = len(discoveries.urls)
    _, key = await _key(client, admin, team, "plain")

    for _ in range(3):
        await _registry(client, key)

    assert len(discoveries.urls) == before  # the explicit discovery, and nothing more


# ── visibility and tenancy ───────────────────────────────────────────────────


async def test_a_global_server_appears_with_its_origin(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    """Resolved through the one visibility union, so a global server reaches a team
    with no grant — the spelling `server.team_id == team_id` would drop it (D5)."""
    admin = await _admin(client)
    team = await _team(client, admin)
    created = await client.post(
        "/platform/mcp-servers",
        json={"name": "shared", "url": SERVER_URL, "auth": "t0ken"},
        headers=_bearer(admin),
    )
    assert created.status_code == HTTP_201_CREATED, created.text
    _, key = await _key(client, admin, team, "plain")

    data = await _registry(client, key)

    assert [(e["id"], e["origin"], e["requires_auth"]) for e in data] == [
        ("shared", "global", True)
    ]
    # The token the gateway holds for it is not in the response.
    assert "t0ken" not in (await client.get("/v1/mcp/servers", headers=_bearer(key))).text


async def test_a_detached_server_disappears_from_the_key_that_saw_it(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    created = await client.post(
        "/platform/mcp-servers",
        json={"name": "shared", "url": SERVER_URL},
        headers=_bearer(admin),
    )
    server = created.json()["id"]
    _, key = await _key(client, admin, team, "plain")
    assert [e["id"] for e in await _registry(client, key)] == ["shared"]

    detached = await client.delete(f"/teams/{team}/mcp-servers/{server}", headers=_bearer(admin))

    assert detached.json() == {"outcome": "detached"}
    assert await _registry(client, key) == []


async def test_a_disabled_server_is_not_offered(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    """`enabled` is a kill switch, so it has to stop the registry too — otherwise
    disabling a server would leave clients still being told to use it."""
    admin = await _admin(client)
    team = await _team(client, admin)
    server = await _server(client, admin, team, "github")
    await _discover(client, admin, team, server)
    _, key = await _key(client, admin, team, "plain")
    assert len(await _registry(client, key)) == 1

    await client.patch(
        f"/teams/{team}/mcp-servers/{server}", json={"enabled": False}, headers=_bearer(admin)
    )

    assert await _registry(client, key) == []


async def test_a_key_sees_nothing_of_another_teams_registry(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    admin = await _admin(client)
    owning = await _team(client, admin)
    other = await _team(client, admin)
    await _server(client, admin, owning, "github")
    _, outsider = await _key(client, admin, other, "outsider")

    assert await _registry(client, outsider) == []


async def test_the_endpoint_requires_an_api_key(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    anonymous = await client.get("/v1/mcp/servers")

    assert anonymous.status_code == HTTP_401_UNAUTHORIZED
