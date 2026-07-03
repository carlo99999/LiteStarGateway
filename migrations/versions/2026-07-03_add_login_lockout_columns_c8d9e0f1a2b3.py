"""add login lockout columns to user_account

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-07-03 12:00:00.000000

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
revision = "c8d9e0f1a2b3"
down_revision = "b7c8d9e0f1a2"
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
    # Existing accounts start with a clean slate (no failures, not locked).
    with op.batch_alter_table("user_account", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "failed_login_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
            )
        )
        batch_op.add_column(
            sa.Column("locked_until", sa.DateTimeUTC(timezone=True), nullable=True)
        )


def schema_downgrades() -> None:
    """schema downgrade migrations go here."""
    with op.batch_alter_table("user_account", schema=None) as batch_op:
        batch_op.drop_column("locked_until")
        batch_op.drop_column("failed_login_attempts")


def data_upgrades() -> None:
    """Add any optional data upgrade migrations here!"""


def data_downgrades() -> None:
    """Add any optional data downgrade migrations here!"""
