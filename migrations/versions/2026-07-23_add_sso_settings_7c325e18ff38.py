"""add sso_settings

Revision ID: 7c325e18ff38
Revises: c83e4a1b7d52
Create Date: 2026-07-23 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from advanced_alchemy.types import GUID, DateTimeUTC
from alembic import op

revision = "7c325e18ff38"
down_revision = "c83e4a1b7d52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A singleton table: at most one row, enforced at the application layer
    # (the repository always reads/writes "the" row, never filters by id).
    # No DB-level singleton constraint exists elsewhere in this schema to
    # mirror, so this is a fresh, minimal pattern rather than an established one.
    op.create_table(
        "sso_settings",
        sa.Column("id", GUID(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("discovery_url", sa.String(), nullable=True),
        sa.Column("client_id", sa.String(), nullable=True),
        sa.Column("encrypted_client_secret", sa.String(), nullable=True),
        sa.Column("key_id", GUID(length=16), nullable=True),
        sa.Column("scopes", sa.String(), nullable=False),
        sa.Column("admin_groups", sa.JSON(), nullable=False),
        sa.Column("default_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("team_mapping", sa.JSON(), nullable=False),
        sa.Column("redirect_uri", sa.String(), nullable=True),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.Column("created_at", DateTimeUTC(timezone=True), nullable=False),
        sa.Column("updated_at", DateTimeUTC(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["key_id"],
            ["secret_key.id"],
            name=op.f("fk_sso_settings_key_id_secret_key"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sso_settings")),
    )


def downgrade() -> None:
    op.drop_table("sso_settings")
