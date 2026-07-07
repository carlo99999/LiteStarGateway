"""add routing_decision text columns (judge distillation export)

Revision ID: b0c1d2e3f4a5
Revises: f8a9b0c1d2e3
Create Date: 2026-07-07 22:00:00.000000

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
revision = "b0c1d2e3f4a5"
down_revision = "f8a9b0c1d2e3"
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
    # Populated only for judge decisions / hybrid escalations (§S6).
    with op.batch_alter_table("routing_decision", schema=None) as batch_op:
        batch_op.add_column(sa.Column("user_text", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("system_prompt", sa.String(), nullable=True))


def schema_downgrades() -> None:
    """schema downgrade migrations go here."""
    with op.batch_alter_table("routing_decision", schema=None) as batch_op:
        batch_op.drop_column("system_prompt")
        batch_op.drop_column("user_text")


def data_upgrades() -> None:
    """Add any optional data upgrade migrations here!"""


def data_downgrades() -> None:
    """Add any optional data downgrade migrations here!"""
