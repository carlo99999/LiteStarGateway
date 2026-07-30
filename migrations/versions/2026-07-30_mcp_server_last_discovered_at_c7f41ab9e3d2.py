"""mcp_server.last_discovered_at

Distinguishes "discovery never ran" from "it ran and the server offers nothing".
Both are an empty `mcp_tool` set, so without this column the console cannot tell
a server nobody queried from one that genuinely has no tools — and the inventory
TTL cannot apply to the second, because every refresh looks like the first.

Nullable and additive: existing rows read as never-discovered, which is the
truthful answer for a server registered before this column existed.

Revision ID: c7f41ab9e3d2
Revises: d2b956dbc5ae
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7f41ab9e3d2"
down_revision = "d2b956dbc5ae"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mcp_server",
        sa.Column("last_discovered_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mcp_server", "last_discovered_at")
