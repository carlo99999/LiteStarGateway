"""add per-team budget alert channel config

Revision ID: b1d4e8f2a7c9
Revises: 9c3d5f7a1b6e
Create Date: 2026-07-27 14:00:00.000000

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
revision = "b1d4e8f2a7c9"
down_revision = "9c3d5f7a1b6e"
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
    # Both columns are nullable (a team may configure a webhook, an email,
    # both, or neither), so existing rows need no server_default backfill —
    # NULL is the correct "not configured" value.
    with op.batch_alter_table("team_budget", schema=None) as batch_op:
        batch_op.add_column(sa.Column("alert_webhook_url", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("alert_email", sa.String(), nullable=True))


def schema_downgrades() -> None:
    """schema downgrade migrations go here."""
    with op.batch_alter_table("team_budget", schema=None) as batch_op:
        batch_op.drop_column("alert_email")
        batch_op.drop_column("alert_webhook_url")


def data_upgrades() -> None:
    """Add any optional data upgrade migrations here!"""


def data_downgrades() -> None:
    """Add any optional data downgrade migrations here!"""
