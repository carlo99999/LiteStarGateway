"""add pending budget alert claim lease

Revision ID: d1054688b7c6
Revises: e227e2928d5e
Create Date: 2026-07-28 00:23:26.362030

"""

import warnings
from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from advanced_alchemy.types import Bool, EncryptedString, EncryptedText, GUID, JsonB, ORA_JSONB, DateTimeUTC, StoredObject, PasswordHash, FernetBackend, TOTPSecret, OneTimeCode
from advanced_alchemy.types.encrypted_string import PGCryptoBackend
from advanced_alchemy.types.password_hash.argon2 import Argon2Hasher
from advanced_alchemy.types.password_hash.passlib import PasslibHasher
from advanced_alchemy.types.password_hash.pwdlib import PwdlibHasher
from sqlalchemy import Text  # noqa: F401


if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ("downgrade", "upgrade", "schema_upgrades", "schema_downgrades", "data_upgrades", "data_downgrades")

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
revision = 'd1054688b7c6'
down_revision = 'e227e2928d5e'
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
    """ISSUE-026: a dispatch lease on each pending alert.

    Two replicas used to select, send and only then delete the same row, so a
    fully successful delivery could still be duplicated. The dispatcher now
    claims a row with an atomic compare-and-swap on this column before sending.
    Nullable with no default: an existing row is simply unclaimed."""
    with op.batch_alter_table('pending_budget_alert', schema=None) as batch_op:
        batch_op.add_column(sa.Column('claimed_until', sa.DateTimeUTC(timezone=True), nullable=True))

def schema_downgrades() -> None:
    """schema downgrade migrations go here."""
    with op.batch_alter_table('pending_budget_alert', schema=None) as batch_op:
        batch_op.drop_column('claimed_until')

def data_upgrades() -> None:
    """Add any optional data upgrade migrations here!"""

def data_downgrades() -> None:
    """Add any optional data downgrade migrations here!"""
