"""Shared DB-url fixture for the model persistence tests.

This is the subset the Postgres CI job runs (see `.github/workflows/ci.yml`): it
exercises real schema/persistence (JSON params, unique constraints, encrypted
credential storage), which is exactly where a migration valid under SQLite's
permissive typing can break under Postgres. When `DATABASE_URL` points at
Postgres (the CI job), each test gets its own throwaway database so tests stay
isolated from each other, mirroring the per-test SQLite file used otherwise.
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
