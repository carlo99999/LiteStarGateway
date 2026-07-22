"""Migration contract for immutable router revisions (R11 ISSUE-011)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from advanced_alchemy.alembic.commands import AlembicCommandConfig
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine

PARENT = "f52a1c9d0b34"  # pragma: allowlist secret
HEAD = "a61d7e3c9b20"  # pragma: allowlist secret


def _config(path: Path) -> AlembicCommandConfig:
    return AlembicCommandConfig(
        engine=create_async_engine(f"sqlite+aiosqlite:///{path}"),
        version_table_name="alembic_version",
        file_="alembic.ini",
    )


def _upgrade(path: Path, revision: str) -> None:
    command.upgrade(_config(path), revision)


def _downgrade(path: Path, revision: str) -> None:
    command.downgrade(_config(path), revision)


def _seed_router(
    connection: sqlite3.Connection, *, candidate: str = "candidate"
) -> tuple[UUID, UUID]:
    now = datetime.now(UTC).isoformat()
    model_id, router_id = uuid4(), uuid4()
    connection.execute(
        """
        INSERT INTO model (
            id, team_id, name, provider, credential_id, type, provider_model_id,
            params, params_enforced, enabled, created_at, updated_at
        ) VALUES (?, NULL, ?, 'openai', ?, 'chat', 'gpt-4o', '{}', '{}', 1, ?, ?)
        """,
        (model_id.bytes, candidate, uuid4().bytes, now, now),
    )
    connection.execute(
        """
        INSERT INTO router (
            id, team_id, name, candidates, default_model, strategy,
            strategy_config, enabled, created_at, updated_at
        ) VALUES (?, NULL, 'smart', ?, ?, 'complexity', '{}', 1, ?, ?)
        """,
        (
            router_id.bytes,
            json.dumps([{"model_name": candidate, "description": "", "quality_tier": "SIMPLE"}]),
            candidate,
            now,
            now,
        ),
    )
    for alias, model_ref, router_ref in (
        (candidate, model_id.bytes, None),
        ("smart", None, router_id.bytes),
    ):
        connection.execute(
            """
            INSERT INTO callable_alias (
                id, team_id, alias, unavailable, model_id, router_id,
                model_grant_id, router_grant_id, created_at, updated_at
            ) VALUES (?, NULL, ?, 0, ?, ?, NULL, NULL, ?, ?)
            """,
            (uuid4().bytes, alias, model_ref, router_ref, now, now),
        )
    connection.commit()
    return model_id, router_id


def test_backfill_binds_candidate_id_and_router_head(tmp_path: Path) -> None:
    path = tmp_path / "backfill.db"
    _upgrade(path, PARENT)
    with sqlite3.connect(path) as connection:
        model_id, router_id = _seed_router(connection)

    _upgrade(path, HEAD)

    with sqlite3.connect(path) as connection:
        revision_id, candidates, default_model_id = connection.execute(
            "SELECT id, candidates, default_model_id FROM router_revision"
        ).fetchone()
        current = connection.execute(
            "SELECT current_revision_id FROM router WHERE id = ?", (router_id.bytes,)
        ).fetchone()[0]
        router_fk = next(
            row
            for row in connection.execute("PRAGMA foreign_key_list(router_grant)")
            if row[2] == "router" and row[3] == "router_id"
        )
    assert json.loads(candidates)[0]["model_id"] == str(model_id)
    assert default_model_id == model_id.bytes
    assert current == revision_id
    assert router_fk[6] == "NO ACTION"


def test_unresolved_candidate_aborts_before_revision_ddl(tmp_path: Path) -> None:
    path = tmp_path / "invalid.db"
    _upgrade(path, PARENT)
    with sqlite3.connect(path) as connection:
        _seed_router(connection, candidate="known")
        connection.execute(
            "UPDATE router SET candidates = ?, default_model = 'missing'",
            (json.dumps([{"model_name": "missing", "quality_tier": "SIMPLE"}]),),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="preflight failed"):
        _upgrade(path, HEAD)

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "router_revision" not in tables


def test_downgrade_blocks_immutable_history_before_ddl(tmp_path: Path) -> None:
    path = tmp_path / "history.db"
    _upgrade(path, PARENT)
    with sqlite3.connect(path) as connection:
        _seed_router(connection)
    _upgrade(path, HEAD)
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """
            SELECT router_id, candidates, default_model_id, default_model_name,
                   strategy, strategy_config, shadow_strategy, enabled
              FROM router_revision
            """
        ).fetchone()
        now = datetime.now(UTC).isoformat()
        connection.execute(
            """
            INSERT INTO router_revision (
                id, router_id, revision_number, candidates, default_model_id,
                default_model_name, strategy, strategy_config, shadow_strategy,
                enabled, created_at, updated_at
            ) VALUES (?, ?, 2, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (uuid4().bytes, *row, now, now),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="downgrade blocked"):
        _downgrade(path, PARENT)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM router_revision").fetchone()[0] == 2
