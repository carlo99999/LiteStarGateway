"""Root `database_url` fixture, shared by every client/persistence fixture.

When `DATABASE_URL` points at Postgres (the CI Postgres job), each test gets its
own throwaway database — real schema/persistence semantics (JSON params, unique
constraints, encrypted credential storage, aggregate SQL) exercised where a
migration valid under SQLite's permissive typing could break on Postgres.
Otherwise it hands out a per-test SQLite file, so local runs stay zero-config.
Test bodies that poke the DB file directly (e.g. sqlite3) keep their own SQLite
fixture on purpose.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.fixture
async def database_url(tmp_path: Path) -> AsyncIterator[str]:
    base_dsn = os.environ.get("DATABASE_URL")
    if not base_dsn or not base_dsn.startswith("postgresql"):
        yield f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
        return

    # Postgres: create a throwaway database for this test only, then drop it.
    db_name = f"test_{uuid.uuid4().hex[:12]}"
    admin_engine = create_async_engine(base_dsn, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        try:
            # render_as_string(hide_password=False): plain str(URL) masks the
            # password as '***', which would reach the app as a literal password
            # and fail auth against the throwaway database.
            yield make_url(base_dsn).set(database=db_name).render_as_string(hide_password=False)
        finally:
            async with admin_engine.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
    finally:
        await admin_engine.dispose()
