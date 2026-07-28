"""Two independent sessions over one database — the closest a test gets to two
replicas.

A transaction-per-test rollback strategy would be faster but would make this
impossible: the multi-replica protocols (the budget-alert outbox claim, and the
reservation store next) are only interesting when two sessions can see each
other's *commits*.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from advanced_alchemy.extensions.litestar import base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@asynccontextmanager
async def two_sessions_over_one_database(
    path: Path,
) -> AsyncIterator[tuple[AsyncSession, AsyncSession]]:
    """Create the schema once at `path` and yield two independent sessions."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(base.UUIDAuditBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as first, maker() as second:
            yield first, second
    finally:
        await engine.dispose()
