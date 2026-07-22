"""unify callable alias registry

Revision ID: f52a1c9d0b34
Revises: d41e8f6a2c10
Create Date: 2026-07-22 16:25:00
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from advanced_alchemy.types import GUID, DateTimeUTC
from alembic import op

revision = "f52a1c9d0b34"
down_revision = "d41e8f6a2c10"
branch_labels = None
depends_on = None


def _bindings_sql(bind: sa.Connection) -> sa.TextClause:
    # PostgreSQL resolves UNION columns pairwise. Two leading bare NULL values
    # become text before the UUID grant ids are considered, so type the empty
    # branch explicitly there. SQLite does not understand its UUID type name.
    empty_grant_id = "CAST(NULL AS UUID)" if bind.dialect.name == "postgresql" else "NULL"
    return sa.text(
        f"""
    SELECT team_id, name AS alias, 'model' AS kind, id AS resource_id,
           {empty_grant_id} AS grant_id
      FROM model
    UNION ALL
    SELECT team_id, name AS alias, 'router' AS kind, id AS resource_id,
           {empty_grant_id} AS grant_id
      FROM router
    UNION ALL
    SELECT team_id, alias, 'model' AS kind, model_id AS resource_id,
           id AS grant_id
      FROM model_grant
    UNION ALL
    SELECT team_id, alias, 'router' AS kind, router_id AS resource_id,
           id AS grant_id
      FROM router_grant
    """
    )


def _source(row: sa.RowMapping) -> str:
    grant = f" grant={row['grant_id']}" if row["grant_id"] is not None else ""
    return f"{row['kind']} resource={row['resource_id']}{grant}"


def _preflight(rows: list[sa.RowMapping]) -> None:
    grouped: dict[tuple[object, str], list[sa.RowMapping]] = defaultdict(list)
    for row in rows:
        grouped[(row["team_id"], row["alias"])].append(row)
    collisions = [
        (scope, alias, values) for (scope, alias), values in grouped.items() if len(values) > 1
    ]
    if not collisions:
        return
    details = []
    for scope, alias, values in collisions[:50]:
        scope_label = "global" if scope is None else f"team={scope}"
        details.append(
            f"{scope_label} alias={alias!r}: " + ", ".join(_source(value) for value in values)
        )
    remainder = len(collisions) - len(details)
    if remainder:
        details.append(f"... and {remainder} more collision(s)")
    raise RuntimeError(
        "Callable alias registry preflight failed; resolve these collisions before retrying:\n"
        + "\n".join(details)
    )


def _lock_source_tables(bind: sa.Connection) -> None:
    """Freeze legacy writers for the snapshot/backfill transaction on Postgres."""
    if bind.dialect.name == "postgresql":
        # SHARE conflicts with the ROW EXCLUSIVE lock taken by legacy
        # INSERT/UPDATE/DELETE statements and is held until Alembic commits.
        bind.execute(sa.text("LOCK TABLE model, router, model_grant, router_grant IN SHARE MODE"))
    elif bind.dialect.name == "sqlite":
        # SQLite ignores SELECT FOR UPDATE/table locks. A no-op write promotes
        # Alembic's deferred transaction to the single writer transaction before
        # the snapshot; the lock remains held through DDL and backfill.
        bind.execute(sa.text("UPDATE model SET name = name WHERE 0"))


def _lock_registry_for_downgrade(bind: sa.Connection) -> None:
    """Serialize the tombstone check with every registry writer."""
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("LOCK TABLE callable_alias IN ACCESS EXCLUSIVE MODE"))
    elif bind.dialect.name == "sqlite":
        bind.execute(sa.text("UPDATE callable_alias SET alias = alias WHERE 0"))


def upgrade() -> None:
    bind = op.get_bind()
    _lock_source_tables(bind)
    source_rows = list(bind.execute(_bindings_sql(bind)).mappings())
    _preflight(source_rows)

    op.create_table(
        "callable_alias",
        sa.Column("id", GUID(length=16), nullable=False),
        sa.Column("team_id", GUID(length=16), nullable=True),
        sa.Column("alias", sa.String(), nullable=False),
        sa.Column("unavailable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("model_id", GUID(length=16), nullable=True),
        sa.Column("router_id", GUID(length=16), nullable=True),
        sa.Column("model_grant_id", GUID(length=16), nullable=True),
        sa.Column("router_grant_id", GUID(length=16), nullable=True),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.Column("created_at", DateTimeUTC(timezone=True), nullable=False),
        sa.Column("updated_at", DateTimeUTC(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(unavailable AND model_id IS NULL AND router_id IS NULL "
            "AND model_grant_id IS NULL AND router_grant_id IS NULL) OR "
            "(NOT unavailable AND ((model_id IS NOT NULL AND router_id IS NULL) OR "
            "(model_id IS NULL AND router_id IS NOT NULL)))",
            name=op.f("ck_callable_alias_state"),
        ),
        sa.CheckConstraint(
            "model_grant_id IS NULL OR (model_id IS NOT NULL AND router_grant_id IS NULL)",
            name=op.f("ck_callable_alias_model_grant_target"),
        ),
        sa.CheckConstraint(
            "router_grant_id IS NULL OR (router_id IS NOT NULL AND model_grant_id IS NULL)",
            name=op.f("ck_callable_alias_router_grant_target"),
        ),
        sa.CheckConstraint(
            "(model_grant_id IS NULL AND router_grant_id IS NULL) OR team_id IS NOT NULL",
            name=op.f("ck_callable_alias_grant_team_scope"),
        ),
        sa.ForeignKeyConstraint(
            ["team_id"],
            ["team.id"],
            name=op.f("fk_callable_alias_team_id_team"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["model.id"],
            name=op.f("fk_callable_alias_model_id_model"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["router_id"],
            ["router.id"],
            name=op.f("fk_callable_alias_router_id_router"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_grant_id"],
            ["model_grant.id"],
            name=op.f("fk_callable_alias_model_grant_id_model_grant"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["router_grant_id"],
            ["router_grant.id"],
            name=op.f("fk_callable_alias_router_grant_id_router_grant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_callable_alias")),
        sa.UniqueConstraint("team_id", "alias", name=op.f("uq_callable_alias_team_id")),
        sa.UniqueConstraint("model_grant_id", name=op.f("uq_callable_alias_model_grant_id")),
        sa.UniqueConstraint("router_grant_id", name=op.f("uq_callable_alias_router_grant_id")),
    )
    op.create_index(op.f("ix_callable_alias_team_id"), "callable_alias", ["team_id"])
    op.create_index(op.f("ix_callable_alias_alias"), "callable_alias", ["alias"])
    op.create_index(op.f("ix_callable_alias_model_id"), "callable_alias", ["model_id"])
    op.create_index(op.f("ix_callable_alias_router_id"), "callable_alias", ["router_id"])
    op.create_index(
        "uq_global_callable_alias",
        "callable_alias",
        ["alias"],
        unique=True,
        sqlite_where=sa.text("team_id IS NULL"),
        postgresql_where=sa.text("team_id IS NULL"),
    )
    op.create_index(
        "uq_callable_alias_direct_model",
        "callable_alias",
        ["model_id"],
        unique=True,
        sqlite_where=sa.text("model_id IS NOT NULL AND model_grant_id IS NULL"),
        postgresql_where=sa.text("model_id IS NOT NULL AND model_grant_id IS NULL"),
    )
    op.create_index(
        "uq_callable_alias_direct_router",
        "callable_alias",
        ["router_id"],
        unique=True,
        sqlite_where=sa.text("router_id IS NOT NULL AND router_grant_id IS NULL"),
        postgresql_where=sa.text("router_id IS NOT NULL AND router_grant_id IS NULL"),
    )

    table = sa.table(
        "callable_alias",
        sa.column("id", GUID(length=16)),
        sa.column("team_id", GUID(length=16)),
        sa.column("alias", sa.String()),
        sa.column("unavailable", sa.Boolean()),
        sa.column("model_id", GUID(length=16)),
        sa.column("router_id", GUID(length=16)),
        sa.column("model_grant_id", GUID(length=16)),
        sa.column("router_grant_id", GUID(length=16)),
        sa.column("created_at", DateTimeUTC(timezone=True)),
        sa.column("updated_at", DateTimeUTC(timezone=True)),
    )
    now = datetime.now(UTC)
    payload = []
    for row in source_rows:
        is_model = row["kind"] == "model"
        payload.append(
            {
                "id": uuid4(),
                "team_id": row["team_id"],
                "alias": row["alias"],
                "unavailable": False,
                "model_id": row["resource_id"] if is_model else None,
                "router_id": None if is_model else row["resource_id"],
                "model_grant_id": row["grant_id"] if is_model else None,
                "router_grant_id": None if is_model else row["grant_id"],
                "created_at": now,
                "updated_at": now,
            }
        )
    if payload:
        op.bulk_insert(table, payload)
    count = bind.scalar(sa.text("SELECT COUNT(*) FROM callable_alias"))
    if count != len(source_rows):
        raise RuntimeError(
            f"Callable alias registry backfill mismatch: expected {len(source_rows)}, got {count}"
        )


def downgrade() -> None:
    bind = op.get_bind()
    _lock_registry_for_downgrade(bind)
    tombstones = bind.scalar(sa.text("SELECT COUNT(*) FROM callable_alias WHERE unavailable"))
    if tombstones:
        raise RuntimeError(
            "Callable alias registry downgrade blocked: unavailable alias slots "
            "cannot be represented by the legacy schema. Explicitly reallocate or "
            "remediate every tombstone before retrying the downgrade."
        )
    op.drop_table("callable_alias")
