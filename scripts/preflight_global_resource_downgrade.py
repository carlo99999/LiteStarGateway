"""Read-only preflight for downgrading global model/router migrations.

Run with the same ``DATABASE_URL`` used by the gateway.  Exit status 0 means the
historical migrations can restore every global resource to a real origin team;
status 1 means operator remediation is required before any downgrade is run.
"""

from __future__ import annotations

import asyncio
import os
import sys

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

RUNBOOK = "docs/db-migrations.md#downgrading-global-resources"


async def _names(connection: AsyncConnection, statement: str) -> list[str]:
    result = await connection.execute(sa.text(statement))
    return [str(name) for name in result.scalars()]


async def _inspect_resource(
    connection: AsyncConnection,
    table: str,
    *,
    has_origin: bool,
) -> tuple[list[str], int]:
    blockers: list[str] = []
    if not has_origin:
        unowned = await _names(
            connection,
            f"SELECT name FROM {table} WHERE team_id IS NULL ORDER BY name LIMIT 20",
        )
        if unowned:
            blockers.append(f"global {table} with no provenance column: {unowned!r}")
        return blockers, 0

    native = await _names(
        connection,
        f"SELECT name FROM {table} "
        "WHERE team_id IS NULL AND origin_team_id IS NULL ORDER BY name LIMIT 20",
    )
    missing_origins = await _names(
        connection,
        f"SELECT resource.name FROM {table} AS resource "
        "LEFT JOIN team ON team.id = resource.origin_team_id "
        "WHERE resource.team_id IS NULL AND resource.origin_team_id IS NOT NULL "
        "AND team.id IS NULL ORDER BY resource.name LIMIT 20",
    )
    collisions = await _names(
        connection,
        f"SELECT global_resource.name FROM {table} AS global_resource "
        f"JOIN {table} AS local_resource "
        "ON local_resource.team_id = global_resource.origin_team_id "
        "AND local_resource.name = global_resource.name "
        "AND local_resource.id <> global_resource.id "
        "WHERE global_resource.team_id IS NULL "
        "ORDER BY global_resource.name LIMIT 20",
    )
    restorable = int(
        await connection.scalar(
            sa.text(
                f"SELECT count(*) FROM {table} WHERE team_id IS NULL AND origin_team_id IS NOT NULL"
            )
        )
        or 0
    )

    if native:
        blockers.append(f"native global {table} without origin_team_id: {native!r}")
    if missing_origins:
        blockers.append(
            f"{table} origin_team_id does not reference an existing team: {missing_origins!r}"
        )
    if collisions:
        blockers.append(f"{table} name already exists in its origin team: {collisions!r}")
    return blockers, restorable


async def preflight(database_url: str) -> tuple[list[str], dict[str, int]]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:

            def schema(sync_connection) -> tuple[set[str], dict[str, set[str]]]:
                inspector = sa.inspect(sync_connection)
                tables = set(inspector.get_table_names())
                columns = {
                    table: {column["name"] for column in inspector.get_columns(table)}
                    for table in ("model", "router")
                    if table in tables
                }
                return tables, columns

            tables, columns = await connection.run_sync(schema)
            blockers: list[str] = []
            restorable: dict[str, int] = {}
            for table in ("model", "router"):
                if table not in tables or "team_id" not in columns[table]:
                    continue
                found, count = await _inspect_resource(
                    connection,
                    table,
                    has_origin="origin_team_id" in columns[table],
                )
                blockers.extend(found)
                restorable[table] = count
            return blockers, restorable
    finally:
        await engine.dispose()


def main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL is required", file=sys.stderr)
        return 2
    try:
        blockers, restorable = asyncio.run(preflight(database_url))
    except Exception as exc:
        print(f"ERROR: downgrade preflight could not inspect the database: {exc}", file=sys.stderr)
        return 2

    if blockers:
        print("UNSAFE: global-resource downgrade is blocked before schema DDL.")
        for blocker in blockers:
            print(f"- {blocker}")
        print("Reassign each resource to an existing team or delete it intentionally, then rerun.")
        print(f"Runbook: {RUNBOOK}")
        return 1

    summary = ", ".join(f"{name}={count}" for name, count in sorted(restorable.items()))
    print(f"SAFE: global-resource downgrade preflight passed ({summary or 'no relevant tables'}).")
    print("Promoted global resources will be restored to origin_team_id during downgrade.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
