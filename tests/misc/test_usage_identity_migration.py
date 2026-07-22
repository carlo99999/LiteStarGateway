"""Migration contract for callable usage attribution."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from advanced_alchemy.alembic.commands import AlembicCommandConfig
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine

PARENT = "a61d7e3c9b20"  # pragma: allowlist secret
HEAD = "c83e4a1b7d52"  # pragma: allowlist secret


def _config(path: Path) -> AlembicCommandConfig:
    return AlembicCommandConfig(
        engine=create_async_engine(f"sqlite+aiosqlite:///{path}"),
        version_table_name="alembic_version",
        file_="alembic.ini",
    )


def _upgrade(path: Path, revision: str) -> None:
    command.upgrade(_config(path), revision)


def test_backfill_marks_only_reconstructible_usage_identity(tmp_path: Path) -> None:
    path = tmp_path / "usage-identity.db"
    _upgrade(path, PARENT)
    event_id, team_id, model_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO usage_event (
                id, team_id, api_key_id, model_id, model_name, operation,
                prompt_tokens, completion_tokens, cost, created_at, updated_at
            ) VALUES (?, ?, NULL, ?, 'canonical', 'chat.completions', 2, 3, 1.5, ?, ?)
            """,
            (event_id.bytes, team_id.bytes, model_id.bytes, now, now),
        )
        connection.commit()

    _upgrade(path, HEAD)
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """
            SELECT requested_alias, resolved_model_id, canonical_model_name,
                   callable_origin, source_team_id
              FROM usage_event WHERE id = ?
            """,
            (event_id.bytes,),
        ).fetchone()
    assert row == (None, model_id.bytes, "canonical", None, None)

    command.downgrade(_config(path), PARENT)
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(usage_event)")}
        preserved = connection.execute(
            "SELECT model_id, model_name, cost FROM usage_event WHERE id = ?",
            (event_id.bytes,),
        ).fetchone()
    assert "requested_alias" not in columns
    assert preserved == (model_id.bytes, "canonical", 1.5)
