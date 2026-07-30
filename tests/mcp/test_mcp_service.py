"""Plan 20 S1 — the service rules: the allowlist veto and one verb, two effects.

The allowlist is checked here on write; the dispatch path re-resolves it per call
(S7). Write-time only is the ISSUE-034 defect, so the test that matters most is
the one proving a host outside the list cannot be registered at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.application.mcp_service import McpServerService
from litestar_gateway.domain.authorization import Permission
from litestar_gateway.domain.egress_policy import parse_allowlist
from litestar_gateway.domain.entities import Principal
from litestar_gateway.domain.exceptions import (
    InvalidMcpServer,
    McpServerNotFound,
    PermissionDenied,
)
from litestar_gateway.domain.mcp import ToolEffect
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.mcp_repository import (
    SQLAlchemyMcpServerRepository,
)
from litestar_gateway.infrastructure.persistence.secret_key_repository import (
    SQLAlchemySecretKeyRepository,
)

TEAM = uuid4()
OTHER_TEAM = uuid4()
ALLOWED = "https://tools.internal:8443/mcp"
# The service only forwards it to the permission check, which `FakeTeams`
# answers — same stand-in the guardrail policy-service tests use.
PRINCIPAL = Principal(user=None, api_key=None)


class FakeTeams:
    """Grants or refuses the permission the service asks for, and records it."""

    def __init__(self, *, allow: bool = True) -> None:
        self._allow = allow
        self.asked: list[Permission] = []

    async def ensure_principal_team_permission(self, principal, team_id: UUID, permission):
        self.asked.append(permission)
        if not self._allow:
            raise PermissionDenied(str(permission))
        return None


@pytest.fixture(autouse=True)
def resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tools.internal` does not resolve on a laptop, and the allowlist check
    resolves before matching (which is what makes a CIDR entry meaningful). The
    established pattern in `tests/egress/` is to patch the resolver, so the test
    exercises the policy rather than the network."""
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
    """With a keyring, so the encrypted-auth path is exercised rather than
    skipped — a token that never gets encrypted would not prove `has_auth`."""
    keyring = Keyring(SQLAlchemySecretKeyRepository(session), "salt-key-material", "jwt-secret")
    return SQLAlchemyMcpServerRepository(session, keyring)


def _service(session: AsyncSession, *, teams: FakeTeams | None = None) -> McpServerService:
    return McpServerService(
        _repo(session),
        teams or FakeTeams(),
        allowlist=parse_allowlist(("tools.internal:8443",)),
    )


async def test_a_host_outside_the_allowlist_cannot_be_registered(session: AsyncSession) -> None:
    # The platform's one veto over a team-owned resource.
    with pytest.raises(InvalidMcpServer, match="MCP_ALLOWED_HOSTS|not permitted"):
        await _service(session).create_server(
            PRINCIPAL, TEAM, name="evil", url="https://attacker.example/mcp"
        )


async def test_the_wrong_port_on_an_allowlisted_host_is_a_different_target(
    session: AsyncSession,
) -> None:
    with pytest.raises(InvalidMcpServer):
        await _service(session).create_server(
            PRINCIPAL, TEAM, name="github", url="https://tools.internal:9999/mcp"
        )


async def test_cleartext_and_userinfo_are_refused(session: AsyncSession) -> None:
    service = _service(session)
    with pytest.raises(InvalidMcpServer, match="https"):
        await service.create_server(PRINCIPAL, TEAM, name="a", url="http://tools.internal:8443/mcp")
    with pytest.raises(InvalidMcpServer, match="userinfo"):
        await service.create_server(
            PRINCIPAL,
            TEAM,
            name="b",
            url="https://user:pw@tools.internal:8443/mcp",  # pragma: allowlist secret
        )


async def test_an_allowlisted_target_is_registered_and_reported_without_its_token(
    session: AsyncSession,
) -> None:
    service = _service(session)

    stored = await service.create_server(PRINCIPAL, TEAM, name="github", url=ALLOWED, auth="t0ken")

    assert stored.has_auth is True
    assert "t0ken" not in repr(stored)


async def test_removing_an_owned_server_deletes_it(session: AsyncSession) -> None:
    service = _service(session)
    stored = await service.create_server(PRINCIPAL, TEAM, name="github", url=ALLOWED)

    assert await service.remove_server(PRINCIPAL, TEAM, stored.id) == "deleted"
    assert await service.list_servers(PRINCIPAL, TEAM) == []


async def test_removing_a_global_server_detaches_it_instead(session: AsyncSession) -> None:
    """One verb, two effects. If this ever deletes, a team admin has revoked a
    capability from every other tenant — Round 12's ISSUE-020."""
    repo = _repo(session)
    service = McpServerService(
        repo, FakeTeams(), allowlist=parse_allowlist(("tools.internal:8443",))
    )
    stored = await service.create_server(PRINCIPAL, OTHER_TEAM, name="shared", url=ALLOWED)
    await repo.make_global(stored.id)

    assert await service.remove_server(PRINCIPAL, TEAM, stored.id) == "detached"

    assert await service.list_servers(PRINCIPAL, TEAM) == []
    # Still there for everyone else, and still a resource.
    assert [s.name for s in await service.list_servers(PRINCIPAL, OTHER_TEAM)] == ["shared"]


async def test_a_detached_global_can_be_reattached(session: AsyncSession) -> None:
    repo = _repo(session)
    service = McpServerService(
        repo, FakeTeams(), allowlist=parse_allowlist(("tools.internal:8443",))
    )
    stored = await service.create_server(PRINCIPAL, OTHER_TEAM, name="shared", url=ALLOWED)
    await repo.make_global(stored.id)
    await service.remove_server(PRINCIPAL, TEAM, stored.id)

    reattached = await service.reattach_server(PRINCIPAL, TEAM, stored.id)

    assert reattached.name == "shared"


async def test_a_team_cannot_edit_a_server_it_only_sees(session: AsyncSession) -> None:
    repo = _repo(session)
    service = McpServerService(
        repo, FakeTeams(), allowlist=parse_allowlist(("tools.internal:8443",))
    )
    stored = await service.create_server(PRINCIPAL, OTHER_TEAM, name="shared", url=ALLOWED)
    await repo.make_global(stored.id)

    with pytest.raises(InvalidMcpServer, match="not edited here"):
        await service.update_server(PRINCIPAL, TEAM, stored.id, enabled=False)
    # ...nor relabel a destructive tool as harmless for everybody.
    with pytest.raises(InvalidMcpServer):
        await service.declare_effect(PRINCIPAL, TEAM, stored.id, "delete_repo", ToolEffect.READ)


async def test_another_teams_private_server_is_not_found_rather_than_forbidden(
    session: AsyncSession,
) -> None:
    # 404, not 403: the difference would confirm the resource exists.
    service = _service(session)
    stored = await service.create_server(PRINCIPAL, OTHER_TEAM, name="private", url=ALLOWED)

    with pytest.raises(McpServerNotFound):
        await service.get_server(PRINCIPAL, TEAM, stored.id)


async def test_reads_and_writes_ask_for_different_permissions(session: AsyncSession) -> None:
    teams = FakeTeams()
    service = McpServerService(
        _repo(session),
        teams,
        allowlist=parse_allowlist(("tools.internal:8443",)),
    )

    await service.create_server(PRINCIPAL, TEAM, name="github", url=ALLOWED)
    await service.list_servers(PRINCIPAL, TEAM)

    assert teams.asked == [Permission.TOOLS_MANAGE, Permission.TOOLS_READ]


async def test_a_refused_permission_stops_the_write(session: AsyncSession) -> None:
    service = McpServerService(
        _repo(session),
        FakeTeams(allow=False),
        allowlist=parse_allowlist(("tools.internal:8443",)),
    )

    with pytest.raises(PermissionDenied):
        await service.create_server(PRINCIPAL, TEAM, name="github", url=ALLOWED)
