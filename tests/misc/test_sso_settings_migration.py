"""Migration contract for the `sso_settings` singleton table."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from advanced_alchemy.alembic.commands import AlembicCommandConfig
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine

PARENT = "c83e4a1b7d52"  # pragma: allowlist secret
HEAD = "7c325e18ff38"  # pragma: allowlist secret


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


def _table_columns(path: Path, table: str) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        connection.close()
    return {row[1] for row in rows}


def test_upgrade_creates_the_expected_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "sso.db"
    _upgrade(db_path, PARENT)
    _upgrade(db_path, HEAD)

    columns = _table_columns(db_path, "sso_settings")
    assert columns == {
        "id",
        "enabled",
        "discovery_url",
        "client_id",
        "encrypted_client_secret",
        "key_id",
        "scopes",
        "admin_groups",
        "default_admin",
        "team_mapping",
        "redirect_uri",
        "sa_orm_sentinel",
        "created_at",
        "updated_at",
    }


def test_downgrade_drops_the_table_and_reupgrade_is_clean(tmp_path: Path) -> None:
    db_path = tmp_path / "sso.db"
    _upgrade(db_path, PARENT)
    _upgrade(db_path, HEAD)
    _downgrade(db_path, PARENT)

    connection = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert "sso_settings" not in tables

    _upgrade(db_path, HEAD)
    assert _table_columns(db_path, "sso_settings")
