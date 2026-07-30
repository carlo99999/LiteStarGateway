"""Plan 20 S1 — the visibility union and the detach that must not delete.

`visible_to` is the one method other code is forbidden to re-implement, so it is
the one that needs the interleavings: a global server nobody granted, a detach
that hides it from one team only, a re-attach, and a promotion that drops grants.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.domain.callable_alias import CallableOrigin
from litestar_gateway.domain.mcp import McpServer, McpTool, ToolEffect
from litestar_gateway.infrastructure.persistence.mcp_repository import (
    SQLAlchemyMcpServerRepository,
)

TEAM = uuid4()
OTHER_TEAM = uuid4()


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mcp.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(base.UUIDAuditBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as opened:
        yield opened
    await engine.dispose()


@pytest.fixture
def repo(session: AsyncSession) -> SQLAlchemyMcpServerRepository:
    return SQLAlchemyMcpServerRepository(session)


def _server(name: str, *, team_id: UUID | None = TEAM) -> McpServer:
    return McpServer(
        id=uuid4(),
        team_id=team_id,
        name=name,
        url="https://tools.internal:8443/mcp",
        enabled=True,
        created_at=datetime.now(UTC),
    )


async def test_a_team_sees_its_own_servers(repo: SQLAlchemyMcpServerRepository) -> None:
    await repo.add(_server("github"))

    visible = await repo.visible_to(TEAM)

    assert [s.name for s in visible] == ["github"]
    assert visible[0].origin is CallableOrigin.OWN
    assert await repo.visible_to(OTHER_TEAM) == []


async def test_a_global_server_is_visible_without_any_grant(
    repo: SQLAlchemyMcpServerRepository,
) -> None:
    # The case a `team_id ==` filter would silently drop.
    await repo.add(_server("shared", team_id=None))

    for team in (TEAM, OTHER_TEAM):
        visible = await repo.visible_to(team)
        assert [s.name for s in visible] == ["shared"]
        assert visible[0].origin is CallableOrigin.GLOBAL


async def test_an_extended_server_is_visible_only_to_the_granted_team(
    repo: SQLAlchemyMcpServerRepository,
) -> None:
    stored = await repo.add(_server("github"))
    await repo.grant(stored.id, OTHER_TEAM)

    granted = await repo.visible_to(OTHER_TEAM)

    assert [s.origin for s in granted] == [CallableOrigin.EXTENDED]
    third = uuid4()
    assert await repo.visible_to(third) == []


async def test_detaching_a_global_hides_it_from_one_team_only(
    repo: SQLAlchemyMcpServerRepository,
) -> None:
    """The ISSUE-020 shape: removing a shared resource for one tenant must not
    remove it for the others."""
    stored = await repo.add(_server("shared", team_id=None))

    await repo.suppress(stored.id, TEAM)

    assert await repo.visible_to(TEAM) == []
    assert [s.name for s in await repo.visible_to(OTHER_TEAM)] == ["shared"]
    # ...and the resource itself is untouched.
    assert await repo.get(stored.id) is not None


async def test_a_detach_is_reversible(repo: SQLAlchemyMcpServerRepository) -> None:
    stored = await repo.add(_server("shared", team_id=None))
    await repo.suppress(stored.id, TEAM)

    assert await repo.unsuppress(stored.id, TEAM)

    assert [s.name for s in await repo.visible_to(TEAM)] == ["shared"]


async def test_detaching_twice_is_idempotent(repo: SQLAlchemyMcpServerRepository) -> None:
    stored = await repo.add(_server("shared", team_id=None))

    await repo.suppress(stored.id, TEAM)
    await repo.suppress(stored.id, TEAM)  # must not raise on the unique constraint

    assert await repo.visible_to(TEAM) == []


async def test_promotion_makes_it_visible_everywhere_and_drops_grants(
    repo: SQLAlchemyMcpServerRepository,
) -> None:
    stored = await repo.add(_server("github"))
    await repo.grant(stored.id, OTHER_TEAM)

    promoted = await repo.make_global(stored.id)

    assert promoted is not None and promoted.is_global
    third = uuid4()
    assert [s.origin for s in await repo.visible_to(third)] == [CallableOrigin.GLOBAL]
    # The grant is gone rather than left as a row nothing reads.
    assert not await repo.revoke_grant(stored.id, OTHER_TEAM)


async def test_a_duplicate_name_is_a_domain_error_not_a_database_one(
    repo: SQLAlchemyMcpServerRepository,
) -> None:
    from litestar_gateway.domain.exceptions import InvalidMcpServer

    await repo.add(_server("github"))

    with pytest.raises(InvalidMcpServer, match="already exists"):
        await repo.add(_server("github"))


async def test_refreshing_the_inventory_keeps_the_declared_effect(
    repo: SQLAlchemyMcpServerRepository,
) -> None:
    """The inventory is a cache of what the server advertises; the effect is
    operator state. Re-reading it from the server would make it a value the
    server controls, which is what "declared, never detected" forbids."""
    stored = await repo.add(_server("github"))
    await repo.replace_tools(
        stored.id,
        [McpTool(id=uuid4(), server_id=stored.id, name="delete_repo", description="", schema={})],
    )
    assert await repo.set_effect(stored.id, "delete_repo", ToolEffect.WRITE)

    # A later discovery re-advertises the tool with a different description.
    refreshed = await repo.replace_tools(
        stored.id,
        [
            McpTool(
                id=uuid4(),
                server_id=stored.id,
                name="delete_repo",
                description="now documented",
                schema={"type": "object"},
            )
        ],
    )

    assert [t.description for t in refreshed] == ["now documented"]
    assert [t.effect for t in refreshed] == [ToolEffect.WRITE]  # not reset to the default


async def test_a_tool_that_disappeared_upstream_is_dropped(
    repo: SQLAlchemyMcpServerRepository,
) -> None:
    stored = await repo.add(_server("github"))
    await repo.replace_tools(
        stored.id,
        [McpTool(id=uuid4(), server_id=stored.id, name="gone", description="", schema={})],
    )

    assert await repo.replace_tools(stored.id, []) == []
