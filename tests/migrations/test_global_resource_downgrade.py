"""Regression coverage for the global model/router downgrade contract."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from advanced_alchemy.alembic.commands import AlembicCommands
from advanced_alchemy.extensions.litestar import SQLAlchemyAsyncConfig
from advanced_alchemy.types import GUID, DateTimeUTC
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine

PRE_GLOBAL_MODELS = "79fc50bbd1a4"  # pragma: allowlist secret
GLOBAL_MODELS = "90e784ecd46b"  # pragma: allowlist secret
MODEL_ORIGIN = "b213468f39d2"  # pragma: allowlist secret
GLOBAL_ROUTERS = "c6366c44d858"  # pragma: allowlist secret


@dataclass(frozen=True)
class SeededResources:
    team_id: UUID
    model_id: UUID | None
    router_id: UUID | None


def _commands(database_url: str) -> AlembicCommands:
    return AlembicCommands(SQLAlchemyAsyncConfig(connection_string=database_url, create_all=False))


async def _upgrade(database_url: str, revision: str) -> None:
    await asyncio.to_thread(_commands(database_url).upgrade, revision)


async def _downgrade(database_url: str, revision: str) -> None:
    await asyncio.to_thread(_commands(database_url).downgrade, revision)


async def _preflight(database_url: str) -> tuple[int, str]:
    script = Path(__file__).parents[2] / "scripts" / "preflight_global_resource_downgrade.py"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        env={**os.environ, "DATABASE_URL": database_url},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return process.returncode or 0, output.decode()


def _column(name: str, type_: sa.types.TypeEngine) -> sa.ColumnClause:
    return sa.column(name, type_)


def _table(name: str, *columns: sa.ColumnClause) -> sa.TableClause:
    return sa.table(name, *columns)


async def _seed_resources(
    database_url: str,
    *,
    model: bool,
    router: bool,
    origin: bool,
    origin_columns_present: bool,
) -> SeededResources:
    """Insert only the columns available at the requested historical revision."""
    now = datetime.now(UTC)
    organization_id = uuid4()
    team_id = uuid4()
    key_id = uuid4()
    credential_id = uuid4()
    model_id = uuid4() if model else None
    router_id = uuid4() if router else None

    organization_table = _table(
        "organization",
        _column("id", GUID(length=16)),
        _column("name", sa.String()),
        _column("created_at", DateTimeUTC(timezone=True)),
        _column("updated_at", DateTimeUTC(timezone=True)),
        _column("tags", sa.JSON()),
    )
    team_table = _table(
        "team",
        _column("id", GUID(length=16)),
        _column("organization_id", GUID(length=16)),
        _column("name", sa.String()),
        _column("created_at", DateTimeUTC(timezone=True)),
        _column("updated_at", DateTimeUTC(timezone=True)),
        _column("tags", sa.JSON()),
    )
    secret_key_table = _table(
        "secret_key",
        _column("id", GUID(length=16)),
        _column("purpose", sa.String()),
        _column("material", sa.String()),
        _column("created_at", DateTimeUTC(timezone=True)),
        _column("updated_at", DateTimeUTC(timezone=True)),
    )
    credential_table = _table(
        "credential",
        _column("id", GUID(length=16)),
        _column("name", sa.String()),
        _column("provider", sa.String()),
        _column("encrypted_values", sa.String()),
        _column("key_id", GUID(length=16)),
        _column("created_at", DateTimeUTC(timezone=True)),
        _column("updated_at", DateTimeUTC(timezone=True)),
    )

    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                organization_table.insert().values(
                    id=organization_id,
                    name=f"org-{organization_id.hex[:8]}",
                    created_at=now,
                    updated_at=now,
                    tags=[],
                )
            )
            await connection.execute(
                team_table.insert().values(
                    id=team_id,
                    organization_id=organization_id,
                    name=f"team-{team_id.hex[:8]}",
                    created_at=now,
                    updated_at=now,
                    tags=[],
                )
            )
            if model:
                await connection.execute(
                    secret_key_table.insert().values(
                        id=key_id,
                        purpose="credential",
                        material="encrypted-test-material",
                        created_at=now,
                        updated_at=now,
                    )
                )
                await connection.execute(
                    credential_table.insert().values(
                        id=credential_id,
                        name=f"credential-{credential_id.hex[:8]}",
                        provider="openai",
                        encrypted_values="encrypted-test-values",
                        key_id=key_id,
                        created_at=now,
                        updated_at=now,
                    )
                )

                model_columns = [
                    _column("id", GUID(length=16)),
                    _column("team_id", GUID(length=16)),
                    _column("name", sa.String()),
                    _column("provider", sa.String()),
                    _column("credential_id", GUID(length=16)),
                    _column("type", sa.String()),
                    _column("provider_model_id", sa.String()),
                    _column("params", sa.JSON()),
                    _column("params_enforced", sa.JSON()),
                    _column("enabled", sa.Boolean()),
                    _column("created_at", DateTimeUTC(timezone=True)),
                    _column("updated_at", DateTimeUTC(timezone=True)),
                ]
                values = {
                    "id": model_id,
                    "team_id": None,
                    "name": "global-model",
                    "provider": "openai",
                    "credential_id": credential_id,
                    "type": "chat",
                    "provider_model_id": "gpt-test",
                    "params": {},
                    "params_enforced": {},
                    "enabled": True,
                    "created_at": now,
                    "updated_at": now,
                }
                if origin_columns_present:
                    model_columns.append(_column("origin_team_id", GUID(length=16)))
                    values["origin_team_id"] = team_id if origin else None
                await connection.execute(_table("model", *model_columns).insert().values(**values))

            if router:
                router_columns = [
                    _column("id", GUID(length=16)),
                    _column("team_id", GUID(length=16)),
                    _column("name", sa.String()),
                    _column("candidates", sa.JSON()),
                    _column("default_model", sa.String()),
                    _column("strategy", sa.String()),
                    _column("strategy_config", sa.JSON()),
                    _column("enabled", sa.Boolean()),
                    _column("created_at", DateTimeUTC(timezone=True)),
                    _column("updated_at", DateTimeUTC(timezone=True)),
                    _column("origin_team_id", GUID(length=16)),
                ]
                await connection.execute(
                    _table("router", *router_columns)
                    .insert()
                    .values(
                        id=router_id,
                        team_id=None,
                        name="global-router",
                        candidates=[],
                        default_model="global-model",
                        strategy="complexity",
                        strategy_config={},
                        enabled=True,
                        created_at=now,
                        updated_at=now,
                        origin_team_id=team_id if origin else None,
                    )
                )
    finally:
        await engine.dispose()

    return SeededResources(team_id=team_id, model_id=model_id, router_id=router_id)


async def _schema(database_url: str) -> tuple[str, set[str], dict[str, set[str]], dict[str, bool]]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            revision = (
                await connection.execute(sa.text("SELECT version_num FROM alembic_versions"))
            ).scalar_one()

            def inspect_schema(sync_connection):
                inspector = sa.inspect(sync_connection)
                tables = set(inspector.get_table_names())
                columns = {
                    table: {column["name"] for column in inspector.get_columns(table)}
                    for table in ("model", "router")
                    if table in tables
                }
                nullable = {
                    table: next(
                        column["nullable"]
                        for column in inspector.get_columns(table)
                        if column["name"] == "team_id"
                    )
                    for table in ("model", "router")
                    if table in tables
                }
                return tables, columns, nullable

            tables, columns, nullable = await connection.run_sync(inspect_schema)
            return revision, tables, columns, nullable
    finally:
        await engine.dispose()


async def _resource_team_id(database_url: str, table_name: str, resource_id: UUID) -> UUID | None:
    resource = _table(
        table_name,
        _column("id", GUID(length=16)),
        _column("team_id", GUID(length=16)),
    )
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return await connection.scalar(
                sa.select(resource.c.team_id).where(resource.c.id == resource_id)
            )
    finally:
        await engine.dispose()


async def _model_origin_team_id(database_url: str, model_id: UUID) -> UUID | None:
    model = _table(
        "model",
        _column("id", GUID(length=16)),
        _column("origin_team_id", GUID(length=16)),
    )
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return await connection.scalar(
                sa.select(model.c.origin_team_id).where(model.c.id == model_id)
            )
    finally:
        await engine.dispose()


async def test_promoted_globals_survive_downgrade_and_upgrade(database_url: str) -> None:
    await _upgrade(database_url, GLOBAL_ROUTERS)
    seeded = await _seed_resources(
        database_url,
        model=True,
        router=True,
        origin=True,
        origin_columns_present=True,
    )

    returncode, output = await _preflight(database_url)
    assert returncode == 0, output
    assert "SAFE" in output
    assert seeded.model_id is not None
    assert seeded.router_id is not None

    await _downgrade(database_url, PRE_GLOBAL_MODELS)

    revision, tables, columns, nullable = await _schema(database_url)
    assert revision == PRE_GLOBAL_MODELS
    assert {"model_grant", "router_grant"}.isdisjoint(tables)
    assert "origin_team_id" not in columns["model"]
    assert "origin_team_id" not in columns["router"]
    assert nullable["model"] is False
    assert nullable["router"] is False
    assert await _resource_team_id(database_url, "model", seeded.model_id) == seeded.team_id
    assert await _resource_team_id(database_url, "router", seeded.router_id) == seeded.team_id

    await _upgrade(database_url, GLOBAL_ROUTERS)

    assert await _resource_team_id(database_url, "model", seeded.model_id) == seeded.team_id
    assert await _resource_team_id(database_url, "router", seeded.router_id) == seeded.team_id


async def test_router_only_downgrade_keeps_global_model_ownership(database_url: str) -> None:
    await _upgrade(database_url, GLOBAL_ROUTERS)
    seeded = await _seed_resources(
        database_url,
        model=True,
        router=True,
        origin=True,
        origin_columns_present=True,
    )
    assert seeded.model_id is not None
    assert seeded.router_id is not None

    await _downgrade(database_url, MODEL_ORIGIN)

    revision, _, columns, nullable = await _schema(database_url)
    assert revision == MODEL_ORIGIN
    assert "origin_team_id" in columns["model"]
    assert "origin_team_id" not in columns["router"]
    assert nullable["model"] is True
    assert nullable["router"] is False
    assert await _resource_team_id(database_url, "model", seeded.model_id) is None
    assert await _model_origin_team_id(database_url, seeded.model_id) == seeded.team_id
    assert await _resource_team_id(database_url, "router", seeded.router_id) == seeded.team_id


@pytest.mark.parametrize("target", [MODEL_ORIGIN, "-1"])
async def test_router_only_downgrade_allows_native_global_model(
    database_url: str, target: str
) -> None:
    await _upgrade(database_url, GLOBAL_ROUTERS)
    seeded = await _seed_resources(
        database_url,
        model=True,
        router=False,
        origin=False,
        origin_columns_present=True,
    )
    assert seeded.model_id is not None

    await _downgrade(database_url, target)

    revision, _, columns, nullable = await _schema(database_url)
    assert revision == MODEL_ORIGIN
    assert "origin_team_id" in columns["model"]
    assert "origin_team_id" not in columns["router"]
    assert nullable["model"] is True
    assert nullable["router"] is False
    assert await _resource_team_id(database_url, "model", seeded.model_id) is None
    assert await _model_origin_team_id(database_url, seeded.model_id) is None


async def test_downgrade_to_model_global_revision_preflights_model_before_router_ddl(
    database_url: str,
) -> None:
    await _upgrade(database_url, GLOBAL_ROUTERS)
    await _seed_resources(
        database_url,
        model=True,
        router=False,
        origin=False,
        origin_columns_present=True,
    )

    with pytest.raises(RuntimeError, match=r"native global model.*docs/db-migrations.md"):
        await _downgrade(database_url, GLOBAL_MODELS)

    revision, tables, columns, nullable = await _schema(database_url)
    assert revision == GLOBAL_ROUTERS
    assert "router_grant" in tables
    assert "origin_team_id" in columns["router"]
    assert nullable["router"] is True


@pytest.mark.parametrize("native_resource", ["model", "router"])
async def test_head_downgrade_aborts_before_ddl_for_native_globals(
    database_url: str, native_resource: str
) -> None:
    await _upgrade(database_url, GLOBAL_ROUTERS)
    await _seed_resources(
        database_url,
        model=native_resource == "model",
        router=native_resource == "router",
        origin=False,
        origin_columns_present=True,
    )

    returncode, output = await _preflight(database_url)
    assert returncode == 1
    assert f"native global {native_resource}" in output
    assert "docs/db-migrations.md#downgrading-global-resources" in output

    with pytest.raises(
        RuntimeError,
        match=rf"native global {native_resource}.*docs/db-migrations.md",
    ):
        await _downgrade(database_url, PRE_GLOBAL_MODELS)

    revision, tables, columns, nullable = await _schema(database_url)
    assert revision == GLOBAL_ROUTERS
    assert {"model_grant", "router_grant"}.issubset(tables)
    assert "origin_team_id" in columns["model"]
    assert "origin_team_id" in columns["router"]
    assert nullable["model"] is True
    assert nullable["router"] is True


async def test_model_origin_revision_aborts_before_dropping_provenance(
    database_url: str,
) -> None:
    await _upgrade(database_url, MODEL_ORIGIN)
    await _seed_resources(
        database_url,
        model=True,
        router=False,
        origin=False,
        origin_columns_present=True,
    )

    with pytest.raises(RuntimeError, match=r"native global model.*docs/db-migrations.md"):
        await _downgrade(database_url, GLOBAL_MODELS)

    revision, tables, columns, nullable = await _schema(database_url)
    assert revision == MODEL_ORIGIN
    assert "model_grant" in tables
    assert "origin_team_id" in columns["model"]
    assert nullable["model"] is True


async def test_legacy_model_revision_aborts_before_not_null_ddl(database_url: str) -> None:
    await _upgrade(database_url, GLOBAL_MODELS)
    await _seed_resources(
        database_url,
        model=True,
        router=False,
        origin=False,
        origin_columns_present=False,
    )

    with pytest.raises(RuntimeError, match=r"global model.*no provenance.*docs/db-migrations.md"):
        await _downgrade(database_url, PRE_GLOBAL_MODELS)

    revision, tables, columns, nullable = await _schema(database_url)
    assert revision == GLOBAL_MODELS
    assert "model_grant" in tables
    assert "origin_team_id" not in columns["model"]
    assert nullable["model"] is True


def test_migration_history_has_one_head() -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    assert len(ScriptDirectory.from_config(config).get_heads()) == 1
