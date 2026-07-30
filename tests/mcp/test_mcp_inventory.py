"""Plan 20 S3 — the inventory as a cache: the TTL, and what a refresh must not undo.

Two properties carry this slice's weight at the service level.

**A refresh never undoes a declaration.** An operator classifies `delete_repo` as
destructive; the server later re-advertises it with friendlier hints. If discovery
overwrote the effect, a server could downgrade its own tools by editing its
annotations — which is the whole thing "declared, never detected" forbids. The
mechanism is `replace_tools`, and this asserts it end to end through the service
rather than trusting the repository test.

**Discovery is `tools:manage`, not `tools:read`.** It makes the gateway open a
connection to an operator-supplied endpoint. §2.4 defers discovery of a *proposed*
server to its approval for the same reason: no low-privilege role should hold a
primitive for making the gateway connect somewhere.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.application.mcp_service import McpServerService
from litestar_gateway.domain.authorization import Permission
from litestar_gateway.domain.egress_policy import parse_allowlist
from litestar_gateway.domain.entities import Principal
from litestar_gateway.domain.exceptions import PermissionDenied
from litestar_gateway.domain.mcp import McpServer, McpTool, ToolEffect
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.mcp_repository import (
    SQLAlchemyMcpServerRepository,
)
from litestar_gateway.infrastructure.persistence.secret_key_repository import (
    SQLAlchemySecretKeyRepository,
)

TEAM = uuid4()
ALLOWED = "https://tools.internal:8443/mcp"
PRINCIPAL = Principal(user=None, api_key=None)


class FakeTeams:
    def __init__(self, *, allow: bool = True) -> None:
        self._allow = allow
        self.asked: list[Permission] = []

    async def ensure_principal_team_permission(self, principal, team_id: UUID, permission):
        self.asked.append(permission)
        if not self._allow:
            raise PermissionDenied(str(permission))
        return None


class ScriptedDiscovery:
    """Answers with whatever the test says the server advertises, and counts the
    calls — the TTL is only observable as "no request was made"."""

    def __init__(self, tools: list[tuple[str, ToolEffect]]) -> None:
        self.tools = tools
        self.calls = 0
        self.tokens: list[str | None] = []

    async def list_tools(self, server: McpServer, *, auth: str | None = None) -> list[McpTool]:
        self.calls += 1
        self.tokens.append(auth)
        now = datetime.now(UTC)
        return [
            McpTool(
                id=uuid4(),
                server_id=server.id,
                name=name,
                description="",
                schema={},
                effect=effect,
                discovered_at=now,
            )
            for name, effect in self.tools
        ]


@pytest.fixture(autouse=True)
def resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    import litestar_gateway.application.egress as egress_module

    async def resolve(host: str) -> list[str]:
        return ["10.9.0.7"]

    monkeypatch.setattr(egress_module, "_resolve_host_addresses", resolve)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mcp.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(base.UUIDAuditBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as opened:
        yield opened
    await engine.dispose()


def _repo(session: AsyncSession) -> SQLAlchemyMcpServerRepository:
    keyring = Keyring(SQLAlchemySecretKeyRepository(session), "salt-key-material", "jwt-secret")
    return SQLAlchemyMcpServerRepository(session, keyring)


def _service(
    session: AsyncSession,
    discovery: ScriptedDiscovery,
    *,
    teams: FakeTeams | None = None,
    ttl_seconds: int = 3600,
) -> McpServerService:
    return McpServerService(
        _repo(session),
        teams or FakeTeams(),
        allowlist=parse_allowlist(("tools.internal:8443",)),
        discovery=discovery,
        inventory_ttl_seconds=ttl_seconds,
    )


async def _register(service: McpServerService, *, auth: str | None = None) -> McpServer:
    return await service.create_server(PRINCIPAL, TEAM, name="github", url=ALLOWED, auth=auth)


# ── the declaration survives ─────────────────────────────────────────────────


async def test_a_refresh_does_not_overwrite_a_declared_effect(session: AsyncSession) -> None:
    """The property that makes re-running discovery safe."""
    discovery = ScriptedDiscovery([("delete_repo", ToolEffect.DESTRUCTIVE)])
    service = _service(session, discovery)
    server = await _register(service)
    await service.refresh_inventory(PRINCIPAL, TEAM, server.id)
    # The operator disagrees with the server's own hint and says so.
    await service.declare_effect(PRINCIPAL, TEAM, server.id, "delete_repo", ToolEffect.WRITE)

    # The server now advertises it as harmless.
    discovery.tools = [("delete_repo", ToolEffect.READ)]
    refreshed = await service.refresh_inventory(PRINCIPAL, TEAM, server.id, force=True)

    assert [(t.name, t.effect) for t in refreshed] == [("delete_repo", ToolEffect.WRITE)]


async def test_a_newly_advertised_tool_takes_the_seeded_effect(session: AsyncSession) -> None:
    """The other side of the same rule: a tool nobody has classified yet gets what
    discovery seeded, so the hint is useful without being authoritative."""
    discovery = ScriptedDiscovery([("search", ToolEffect.READ)])
    service = _service(session, discovery)
    server = await _register(service)

    stored = await service.refresh_inventory(PRINCIPAL, TEAM, server.id)

    assert [(t.name, t.effect) for t in stored] == [("search", ToolEffect.READ)]


async def test_a_tool_that_disappeared_upstream_leaves_the_inventory(
    session: AsyncSession,
) -> None:
    discovery = ScriptedDiscovery([("search", ToolEffect.READ), ("gone", ToolEffect.READ)])
    service = _service(session, discovery)
    server = await _register(service)
    await service.refresh_inventory(PRINCIPAL, TEAM, server.id)

    discovery.tools = [("search", ToolEffect.READ)]
    refreshed = await service.refresh_inventory(PRINCIPAL, TEAM, server.id, force=True)

    assert [t.name for t in refreshed] == ["search"]


# ── the TTL ──────────────────────────────────────────────────────────────────


async def test_a_refresh_inside_the_ttl_makes_no_request(session: AsyncSession) -> None:
    """So a console that refreshes on page load does not turn every visit into
    traffic to somebody's tool server."""
    discovery = ScriptedDiscovery([("search", ToolEffect.READ)])
    service = _service(session, discovery)
    server = await _register(service)

    first = await service.refresh_inventory(PRINCIPAL, TEAM, server.id)
    second = await service.refresh_inventory(PRINCIPAL, TEAM, server.id)

    assert discovery.calls == 1
    assert [t.name for t in second] == [t.name for t in first]


async def test_force_asks_again_even_inside_the_ttl(session: AsyncSession) -> None:
    discovery = ScriptedDiscovery([("search", ToolEffect.READ)])
    service = _service(session, discovery)
    server = await _register(service)
    await service.refresh_inventory(PRINCIPAL, TEAM, server.id)

    await service.refresh_inventory(PRINCIPAL, TEAM, server.id, force=True)

    assert discovery.calls == 2


async def test_an_expired_inventory_is_refreshed_without_force(session: AsyncSession) -> None:
    discovery = ScriptedDiscovery([("search", ToolEffect.READ)])
    service = _service(session, discovery, ttl_seconds=0)
    server = await _register(service)

    await service.refresh_inventory(PRINCIPAL, TEAM, server.id)
    await service.refresh_inventory(PRINCIPAL, TEAM, server.id)

    assert discovery.calls == 2


async def test_an_empty_inventory_is_never_treated_as_fresh(session: AsyncSession) -> None:
    """ "We asked and it offers nothing" and "we never asked" look identical in
    storage. Treating the second as fresh would mean the first discovery never
    happens — the server would stay permanently empty."""
    discovery = ScriptedDiscovery([])
    service = _service(session, discovery)
    server = await _register(service)

    await service.refresh_inventory(PRINCIPAL, TEAM, server.id)
    await service.refresh_inventory(PRINCIPAL, TEAM, server.id)

    assert discovery.calls == 2


async def test_an_inventory_with_no_timestamp_is_not_fresh(session: AsyncSession) -> None:
    """Defence against a row written without `discovered_at` — a `max()` over an
    empty list of stamps must not read as "just refreshed"."""
    discovery = ScriptedDiscovery([("search", ToolEffect.READ)])
    service = _service(session, discovery)
    server = await _register(service)
    repo = _repo(session)
    await repo.replace_tools(
        server.id,
        [McpTool(id=uuid4(), server_id=server.id, name="search", description="", schema={})],
    )
    stored = await repo.tools(server.id)
    assert stored and stored[0].discovered_at is not None  # the repo always stamps

    # So construct the degenerate case directly against the predicate.
    assert service._is_fresh([]) is False
    assert (
        service._is_fresh(
            [dataclasses.replace(stored[0], discovered_at=None)],
        )
        is False
    )
    old = dataclasses.replace(stored[0], discovered_at=datetime.now(UTC) - timedelta(days=2))
    assert service._is_fresh([old]) is False


# ── authorization and the token ──────────────────────────────────────────────


async def test_discovery_asks_for_manage_not_read(session: AsyncSession) -> None:
    teams = FakeTeams()
    discovery = ScriptedDiscovery([("search", ToolEffect.READ)])
    service = _service(session, discovery, teams=teams)
    server = await _register(service)
    teams.asked.clear()

    await service.refresh_inventory(PRINCIPAL, TEAM, server.id)

    assert teams.asked == [Permission.TOOLS_MANAGE]


async def test_a_refused_permission_makes_no_request(session: AsyncSession) -> None:
    discovery = ScriptedDiscovery([("search", ToolEffect.READ)])
    permissive = _service(session, discovery)
    server = await _register(permissive)
    refusing = _service(session, discovery, teams=FakeTeams(allow=False))

    with pytest.raises(PermissionDenied):
        await refusing.refresh_inventory(PRINCIPAL, TEAM, server.id)

    assert discovery.calls == 0


async def test_the_stored_token_is_handed_to_the_client_and_nowhere_else(
    session: AsyncSession,
) -> None:
    discovery = ScriptedDiscovery([("search", ToolEffect.READ)])
    service = _service(session, discovery)
    server = await _register(service, auth="pw-must-not-leak")

    await service.refresh_inventory(PRINCIPAL, TEAM, server.id)

    assert discovery.tokens == ["pw-must-not-leak"]
    # The entity the endpoint builds its response from does not carry it.
    listed = await service.list_servers(PRINCIPAL, TEAM)
    assert listed[0].has_auth is True
    assert "pw-must-not-leak" not in repr(listed)


async def test_a_server_the_team_cannot_see_is_not_discoverable(session: AsyncSession) -> None:
    """Visibility first: discovery on another team's server must not even reach
    the point of resolving its host."""
    from litestar_gateway.domain.exceptions import McpServerNotFound

    discovery = ScriptedDiscovery([("search", ToolEffect.READ)])
    other_team = uuid4()
    service = _service(session, discovery)
    server = await service.create_server(PRINCIPAL, other_team, name="private", url=ALLOWED)

    with pytest.raises(McpServerNotFound):
        await service.refresh_inventory(PRINCIPAL, TEAM, server.id)

    assert discovery.calls == 0
