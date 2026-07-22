"""preserve requested and resolved callable identity in usage

Revision ID: c83e4a1b7d52
Revises: a61d7e3c9b20
Create Date: 2026-07-22 18:20:00
"""

from __future__ import annotations

import sqlalchemy as sa
from advanced_alchemy.types import GUID
from alembic import op

revision = "c83e4a1b7d52"
down_revision = "a61d7e3c9b20"
branch_labels = None
depends_on = None


def _add_identity_columns(table: str, *, indexed: bool) -> None:
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.add_column(sa.Column("requested_alias", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("resolved_model_id", GUID(length=16), nullable=True))
        batch_op.add_column(sa.Column("canonical_model_name", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("callable_origin", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("source_team_id", GUID(length=16), nullable=True))
        if indexed:
            batch_op.create_index(
                batch_op.f(f"ix_{table}_requested_alias"), ["requested_alias"], unique=False
            )
            batch_op.create_index(
                batch_op.f(f"ix_{table}_resolved_model_id"),
                ["resolved_model_id"],
                unique=False,
            )
            batch_op.create_index(
                batch_op.f(f"ix_{table}_canonical_model_name"),
                ["canonical_model_name"],
                unique=False,
            )


def upgrade() -> None:
    _add_identity_columns("usage_event", indexed=True)
    _add_identity_columns("pending_usage_event", indexed=False)
    # model_id/model_name were already authoritative billing snapshots. They
    # can be copied safely; the historical requested alias/scope cannot be
    # reconstructed and deliberately remain NULL (reported as "unknown").
    op.execute(
        sa.text(
            "UPDATE usage_event SET resolved_model_id = model_id, canonical_model_name = model_name"
        )
    )
    op.execute(
        sa.text(
            "UPDATE pending_usage_event SET resolved_model_id = model_id, "
            "canonical_model_name = model_name"
        )
    )


def _drop_identity_columns(table: str, *, indexed: bool) -> None:
    with op.batch_alter_table(table, schema=None) as batch_op:
        if indexed:
            batch_op.drop_index(batch_op.f(f"ix_{table}_canonical_model_name"))
            batch_op.drop_index(batch_op.f(f"ix_{table}_resolved_model_id"))
            batch_op.drop_index(batch_op.f(f"ix_{table}_requested_alias"))
        batch_op.drop_column("source_team_id")
        batch_op.drop_column("callable_origin")
        batch_op.drop_column("canonical_model_name")
        batch_op.drop_column("resolved_model_id")
        batch_op.drop_column("requested_alias")


def downgrade() -> None:
    _drop_identity_columns("pending_usage_event", indexed=False)
    _drop_identity_columns("usage_event", indexed=True)
