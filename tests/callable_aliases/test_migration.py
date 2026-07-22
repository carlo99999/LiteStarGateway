"""Migration contract for the persisted callable-alias registry."""

from __future__ import annotations

import asyncio
import runpy
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from advanced_alchemy.alembic.commands import AlembicCommandConfig
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine

from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.persistence.database import create_database

PARENT = "d41e8f6a2c10"  # pragma: allowlist secret
HEAD = "f52a1c9d0b34"  # pragma: allowlist secret


def _config(path: Path) -> AlembicCommandConfig:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    return AlembicCommandConfig(
        engine=engine,
        version_table_name="alembic_version",
        file_="alembic.ini",
    )


def _upgrade(path: Path, revision: str) -> None:
    command.upgrade(_config(path), revision)


def _downgrade(path: Path, revision: str) -> None:
    command.downgrade(_config(path), revision)


def _insert_model(connection: sqlite3.Connection, name: str) -> UUID:
    model_id = uuid4()
    now = datetime.now(UTC).isoformat()
    connection.execute(
        """
        INSERT INTO model (
            id, team_id, name, provider, credential_id, type, provider_model_id,
            params, params_enforced, enabled, created_at, updated_at
        ) VALUES (?, NULL, ?, 'openai', ?, 'chat', 'gpt-4o', '{}', '{}', 1, ?, ?)
        """,
        (model_id.bytes, name, uuid4().bytes, now, now),
    )
    connection.commit()
    return model_id


def _insert_router(connection: sqlite3.Connection, name: str) -> UUID:
    router_id = uuid4()
    now = datetime.now(UTC).isoformat()
    connection.execute(
        """
        INSERT INTO router (
            id, team_id, name, candidates, default_model, strategy,
            strategy_config, enabled, created_at, updated_at
        ) VALUES (?, NULL, ?, '[]', 'unused', 'complexity', '{}', 1, ?, ?)
        """,
        (router_id.bytes, name, now, now),
    )
    connection.commit()
    return router_id


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def test_preflight_collision_aborts_before_registry_ddl(tmp_path: Path) -> None:
    path = tmp_path / "preflight.db"
    _upgrade(path, PARENT)
    with sqlite3.connect(path) as connection:
        _insert_model(connection, "collision")
        _insert_router(connection, "collision")

    with pytest.raises(RuntimeError, match="preflight failed") as exc_info:
        _upgrade(path, HEAD)

    assert "global alias='collision'" in str(exc_info.value)
    with sqlite3.connect(path) as connection:
        assert "callable_alias" not in _tables(connection)
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert revision == PARENT


def test_backfill_and_downgrade_preserve_source_rows(tmp_path: Path) -> None:
    path = tmp_path / "roundtrip.db"
    _upgrade(path, PARENT)
    with sqlite3.connect(path) as connection:
        model_id = _insert_model(connection, "backfilled")

    _upgrade(path, HEAD)
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT alias, model_id, unavailable FROM callable_alias"
        ).fetchone()
    assert row == ("backfilled", model_id.bytes, 0)

    _downgrade(path, PARENT)
    with sqlite3.connect(path) as connection:
        assert "callable_alias" not in _tables(connection)
        source = connection.execute("SELECT name FROM model WHERE id = ?", (model_id.bytes,))
        assert source.fetchone()[0] == "backfilled"


def test_downgrade_with_tombstone_aborts_before_drop(tmp_path: Path) -> None:
    path = tmp_path / "tombstone.db"
    _upgrade(path, PARENT)
    with sqlite3.connect(path) as connection:
        _insert_model(connection, "reserved")
    _upgrade(path, HEAD)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE callable_alias SET unavailable = 1, model_id = NULL WHERE alias = 'reserved'"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="downgrade blocked"):
        _downgrade(path, PARENT)

    with sqlite3.connect(path) as connection:
        assert "callable_alias" in _tables(connection)
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert revision == HEAD


def test_postgres_backfill_locks_every_legacy_source_table() -> None:
    namespace = runpy.run_path(
        "migrations/versions/2026-07-22_unify_callable_alias_registry_f52a1c9d0b34.py"
    )

    class Bind:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement) -> None:
            self.statements.append(str(statement))

    bind = Bind()
    namespace["_lock_source_tables"](bind)

    assert bind.statements == ["LOCK TABLE model, router, model_grant, router_grant IN SHARE MODE"]


def test_postgres_backfill_types_empty_grant_ids_as_uuid() -> None:
    namespace = runpy.run_path(
        "migrations/versions/2026-07-22_unify_callable_alias_registry_f52a1c9d0b34.py"
    )

    class Bind:
        dialect = type("Dialect", (), {"name": "postgresql"})()

    statement = str(namespace["_bindings_sql"](Bind()))

    assert statement.count("CAST(NULL AS UUID) AS grant_id") == 2


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        ("postgresql", "LOCK TABLE callable_alias IN ACCESS EXCLUSIVE MODE"),
        ("sqlite", "UPDATE callable_alias SET alias = alias WHERE 0"),
    ],
)
def test_downgrade_locks_registry_before_tombstone_preflight(dialect: str, expected: str) -> None:
    namespace = runpy.run_path(
        "migrations/versions/2026-07-22_unify_callable_alias_registry_f52a1c9d0b34.py"
    )

    class Bind:
        def __init__(self) -> None:
            self.dialect = type("Dialect", (), {"name": dialect})()
            self.statements: list[str] = []

        def execute(self, statement) -> None:
            self.statements.append(str(statement))

    bind = Bind()
    namespace["_lock_registry_for_downgrade"](bind)

    assert bind.statements == [expected]


def test_dev_create_all_refuses_unmigrated_legacy_schema(tmp_path: Path) -> None:
    path = tmp_path / "legacy-startup.db"
    _upgrade(path, PARENT)
    with sqlite3.connect(path) as connection:
        _insert_model(connection, "legacy")

    async def create_schema() -> None:
        database = create_database(
            Settings(
                database_url=f"sqlite+aiosqlite:///{path}",
                admin_email="admin@example.com",
                master_key="m" * 32,
                jwt_secret="x" * 40,
                salt_key="s" * 32,
            )
        )
        try:
            await database.config.create_all_metadata(cast(Any, None))
        finally:
            await database.config.get_engine().dispose()

    with pytest.raises(RuntimeError, match="database upgrade"):
        asyncio.run(create_schema())
    with sqlite3.connect(path) as connection:
        assert "callable_alias" not in _tables(connection)

    _upgrade(path, HEAD)
    asyncio.run(create_schema())
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT model_id FROM callable_alias WHERE alias = 'legacy'"
            ).fetchone()[0]
            is not None
        )
