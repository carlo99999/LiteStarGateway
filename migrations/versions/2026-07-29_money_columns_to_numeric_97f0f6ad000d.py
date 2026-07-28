"""money columns to numeric

Revision ID: 97f0f6ad000d
Revises: 21bb299a0959
Create Date: 2026-07-29 01:06:43.253699

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
revision = '97f0f6ad000d'
down_revision = '21bb299a0959'
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

# Every column that holds a monetary AMOUNT. Per-token rates are deliberately
# absent: at cost scale a 0.0000005 rate would round to 0.000001, so they move
# to their own, finer scale with the model rate columns.
MONEY_COLUMNS = {
    "usage_event": ["cost"],
    "pending_usage_event": ["cost"],
    "team_budget": ["limit_cost"],
    "pending_budget_alert": ["spend", "limit_cost"],
}


def schema_upgrades() -> None:
    """Money becomes NUMERIC(20, 6) — exact storage and an exact SUM.

    A binary float cannot represent ordinary decimal amounts, so the ledger
    drifted with the number of rows summed and gave an order-dependent total;
    for the budget gate that is the difference between admitting a request and
    refusing it (R3-L15, carried since Round 3).

    Existing values are converted by the database, not re-derived: each stored
    float becomes the NUMERIC nearest to it, which is the same number the
    application was already reading back. No rows are rewritten by hand.
    """
    for table, columns in MONEY_COLUMNS.items():
        with op.batch_alter_table(table, schema=None) as batch_op:
            for column in columns:
                batch_op.alter_column(
                    column,
                    type_=sa.Numeric(20, 6),
                    existing_type=sa.Float(),
                    existing_nullable=False,
                    postgresql_using=f"{column}::numeric(20,6)",
                )

def schema_downgrades() -> None:
    """schema downgrade migrations go here."""
    for table, columns in MONEY_COLUMNS.items():
        with op.batch_alter_table(table, schema=None) as batch_op:
            for column in columns:
                batch_op.alter_column(
                    column,
                    type_=sa.Float(),
                    existing_type=sa.Numeric(20, 6),
                    existing_nullable=False,
                )

def data_upgrades() -> None:
    """Add any optional data upgrade migrations here!"""

def data_downgrades() -> None:
    """Add any optional data downgrade migrations here!"""
