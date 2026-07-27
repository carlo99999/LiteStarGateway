"""add pending_budget_alert outbox

Revision ID: 9c3d5f7a1b6e
Revises: 5e4c132e7869
Create Date: 2026-07-27 12:00:00.000000

"""

import warnings
from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from advanced_alchemy.types import (
    Bool,
    EncryptedString,
    EncryptedText,
    GUID,
    JsonB,
    ORA_JSONB,
    DateTimeUTC,
    StoredObject,
    PasswordHash,
    FernetBackend,
    TOTPSecret,
    OneTimeCode,
)
from advanced_alchemy.types.encrypted_string import PGCryptoBackend
from advanced_alchemy.types.password_hash.argon2 import Argon2Hasher
from advanced_alchemy.types.password_hash.passlib import PasslibHasher
from advanced_alchemy.types.password_hash.pwdlib import PwdlibHasher
from sqlalchemy import Text  # noqa: F401


if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = (
    "downgrade",
    "upgrade",
    "schema_upgrades",
    "schema_downgrades",
    "data_upgrades",
    "data_downgrades",
)

sa.GUID = GUID
sa.Bool = Bool
sa.DateTimeUTC = DateTimeUTC
sa.JsonB = JsonB
sa.ORA_JSONB = ORA_JSONB
sa.EncryptedString = EncryptedString
sa.EncryptedText = EncryptedText
sa.StoredObject = StoredObject
sa.PasswordHash = PasswordHash
sa.Argon2Hasher = Argon2Hasher
sa.PasslibHasher = PasslibHasher
sa.PwdlibHasher = PwdlibHasher
sa.FernetBackend = FernetBackend
sa.PGCryptoBackend = PGCryptoBackend
sa.TOTPSecret = TOTPSecret
sa.OneTimeCode = OneTimeCode

# revision identifiers, used by Alembic.
revision = "9c3d5f7a1b6e"
down_revision = "5e4c132e7869"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning)
        with op.get_context().autocommit_block():
            schema_upgrades()
            data_upgrades()


def downgrade() -> None:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=UserWarning)
        with op.get_context().autocommit_block():
            data_downgrades()
            schema_downgrades()


def schema_upgrades() -> None:
    """schema upgrade migrations go here."""
    op.create_table(
        "pending_budget_alert",
        sa.Column("id", sa.GUID(length=16), nullable=False),
        sa.Column("team_id", sa.GUID(length=16), nullable=False),
        sa.Column("window", sa.String(), nullable=False),
        sa.Column("period_start", sa.DateTimeUTC(timezone=True), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("spend", sa.Float(), nullable=False),
        sa.Column("limit_cost", sa.Float(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTimeUTC(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTimeUTC(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["team_id"], ["team.id"], name=op.f("fk_pending_budget_alert_team_id_team")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pending_budget_alert")),
    )
    with op.batch_alter_table("pending_budget_alert", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_pending_budget_alert_team_id"), ["team_id"], unique=False
        )


def schema_downgrades() -> None:
    """schema downgrade migrations go here."""
    with op.batch_alter_table("pending_budget_alert", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_pending_budget_alert_team_id"))

    op.drop_table("pending_budget_alert")


def data_upgrades() -> None:
    """Add any optional data upgrade migrations here!"""


def data_downgrades() -> None:
    """Add any optional data downgrade migrations here!"""
