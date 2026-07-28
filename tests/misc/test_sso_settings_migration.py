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


# ---------------------------------------------------------------------------
# ISSUE-032: the data migration that neutralises legacy unsafe rows.
# ---------------------------------------------------------------------------

BEFORE_DISABLE = "d1054688b7c6"  # pragma: allowlist secret
DISABLE_UNSAFE = "21bb299a0959"  # pragma: allowlist secret


def _insert_sso_row(path: Path, *, enabled: bool, redirect_uri: str | None) -> str:
    from datetime import UTC, datetime
    from uuid import uuid4

    row_id = uuid4().bytes
    now = datetime.now(UTC).isoformat()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO sso_settings (id, enabled, discovery_url, client_id, scopes, "
            "admin_groups, default_admin, team_mapping, redirect_uri, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                row_id,
                1 if enabled else 0,
                "https://idp.invalid/.well-known/openid-configuration",
                "client-1",
                "openid email",
                "[]",
                0,
                "{}",
                redirect_uri,
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return row_id.hex()


def _enabled_flags(path: Path) -> list[int]:
    connection = sqlite3.connect(path)
    try:
        return [row[0] for row in connection.execute("SELECT enabled FROM sso_settings").fetchall()]
    finally:
        connection.close()


def test_an_enabled_row_without_a_redirect_uri_is_disabled(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    _upgrade(db_path, BEFORE_DISABLE)
    _insert_sso_row(db_path, enabled=True, redirect_uri=None)

    _upgrade(db_path, DISABLE_UNSAFE)

    assert _enabled_flags(db_path) == [0]


def test_a_configuration_with_a_redirect_uri_is_left_alone(tmp_path: Path) -> None:
    db_path = tmp_path / "safe.db"
    _upgrade(db_path, BEFORE_DISABLE)
    _insert_sso_row(db_path, enabled=True, redirect_uri="https://gw.example.com/sso/callback")

    _upgrade(db_path, DISABLE_UNSAFE)

    assert _enabled_flags(db_path) == [1]


def test_a_blank_redirect_uri_counts_as_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "blank.db"
    _upgrade(db_path, BEFORE_DISABLE)
    _insert_sso_row(db_path, enabled=True, redirect_uri="   ")

    _upgrade(db_path, DISABLE_UNSAFE)

    assert _enabled_flags(db_path) == [0]
