"""model rate check constraints

Revision ID: e227e2928d5e
Revises: 47e59bf43231
Create Date: 2026-07-27 22:43:55.286003

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
revision = 'e227e2928d5e'
down_revision = '47e59bf43231'
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

RATE_COLUMNS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_write_cost_per_token",
    "cache_read_cost_per_token",
    "image_cost_per_image",
)


def schema_upgrades() -> None:
    """ISSUE-022: a model rate must never be negative.

    A negative rate makes `domain.pricing.compute_cost` return a credit, which
    settlement writes into the same ledger the budget gate reads. The service
    now refuses one on every write path; these constraints keep a writer that
    bypasses the service from reintroducing it.

    Existing negative rates are clamped to 0.0 FIRST — the constraint cannot be
    created while a violating row exists, and the template runs `data_upgrades`
    after this function, so the normalization lives here rather than there.
    `image_prices` is JSON: no portable CHECK, application validation only.
    """
    for column in RATE_COLUMNS:
        op.execute(f"UPDATE model SET {column} = 0.0 WHERE {column} < 0")
    with op.batch_alter_table("model", schema=None) as batch_op:
        for column in RATE_COLUMNS:
            batch_op.create_check_constraint(
                f"ck_model_{column}_non_neg", f"{column} IS NULL OR {column} >= 0"
            )

def schema_downgrades() -> None:
    """schema downgrade migrations go here."""
    with op.batch_alter_table("model", schema=None) as batch_op:
        for column in RATE_COLUMNS:
            batch_op.drop_constraint(f"ck_model_{column}_non_neg", type_="check")

def data_upgrades() -> None:
    """Add any optional data upgrade migrations here!"""

def data_downgrades() -> None:
    """Add any optional data downgrade migrations here!"""
