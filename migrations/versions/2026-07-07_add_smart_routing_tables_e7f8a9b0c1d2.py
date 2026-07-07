"""add router (virtual model) + routing_decision tables (smart routing)

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-07-07 18:00:00.000000

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
revision = "e7f8a9b0c1d2"
down_revision = "d6e7f8a9b0c1"
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
        "router",
        sa.Column("id", sa.GUID(length=16), nullable=False),
        sa.Column("team_id", sa.GUID(length=16), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column("default_model", sa.String(), nullable=False),
        sa.Column("strategy", sa.String(), nullable=False),
        sa.Column("strategy_config", sa.JSON(), nullable=False),
        sa.Column("shadow_strategy", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTimeUTC(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTimeUTC(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["team.id"], name=op.f("fk_router_team_id_team")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_router")),
        sa.UniqueConstraint("team_id", "name", name=op.f("uq_router_team_id")),
    )
    with op.batch_alter_table("router", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_router_team_id"), ["team_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_router_name"), ["name"], unique=False)

    op.create_table(
        "routing_decision",
        sa.Column("id", sa.GUID(length=16), nullable=False),
        sa.Column("team_id", sa.GUID(length=16), nullable=False),
        sa.Column("router_name", sa.String(), nullable=False),
        sa.Column("strategy", sa.String(), nullable=False),
        sa.Column("chosen_model", sa.String(), nullable=False),
        sa.Column("tier", sa.String(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("signals", sa.JSON(), nullable=False),
        sa.Column("decision_ms", sa.Float(), nullable=False),
        sa.Column("is_shadow", sa.Boolean(), nullable=False),
        sa.Column("fallback_used", sa.Boolean(), nullable=False),
        sa.Column("api_key_id", sa.GUID(length=16), nullable=True),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTimeUTC(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTimeUTC(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routing_decision")),
    )
    with op.batch_alter_table("routing_decision", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_routing_decision_team_id"), ["team_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_routing_decision_router_name"), ["router_name"], unique=False
        )


def schema_downgrades() -> None:
    """schema downgrade migrations go here."""
    with op.batch_alter_table("routing_decision", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_routing_decision_router_name"))
        batch_op.drop_index(batch_op.f("ix_routing_decision_team_id"))
    op.drop_table("routing_decision")
    with op.batch_alter_table("router", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_router_name"))
        batch_op.drop_index(batch_op.f("ix_router_team_id"))
    op.drop_table("router")


def data_upgrades() -> None:
    """Add any optional data upgrade migrations here!"""


def data_downgrades() -> None:
    """Add any optional data downgrade migrations here!"""
