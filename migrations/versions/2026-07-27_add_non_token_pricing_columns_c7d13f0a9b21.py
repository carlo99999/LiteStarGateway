"""add non-token pricing columns

Revision ID: c7d13f0a9b21
Revises: 7750ec93d00f
Create Date: 2026-07-27 10:00:00.000000

Plan 13 Phase 1: image count/size/quality pricing and Anthropic prompt-cache
write/read rates on the model, plus the cache-token and image-count billing
dimensions on the usage ledger and its dead-letter outbox.
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
revision = 'c7d13f0a9b21'
down_revision = '7750ec93d00f'
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
    with op.batch_alter_table('model', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cache_write_cost_per_token', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('cache_read_cost_per_token', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('image_cost_per_image', sa.Float(), nullable=True))
        # server_default '{}' backfills existing rows so the NOT NULL add
        # succeeds on a populated table; new inserts get their value from the
        # ORM default.
        batch_op.add_column(
            sa.Column('image_prices', sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
        )

    with op.batch_alter_table('usage_event', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('cache_write_tokens', sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(
            sa.Column('cache_read_tokens', sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(
            sa.Column('image_count', sa.Integer(), nullable=False, server_default=sa.text("0"))
        )

    with op.batch_alter_table('pending_usage_event', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('cache_write_tokens', sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(
            sa.Column('cache_read_tokens', sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(
            sa.Column('image_count', sa.Integer(), nullable=False, server_default=sa.text("0"))
        )

def schema_downgrades() -> None:
    """schema downgrade migrations go here."""
    with op.batch_alter_table('pending_usage_event', schema=None) as batch_op:
        batch_op.drop_column('image_count')
        batch_op.drop_column('cache_read_tokens')
        batch_op.drop_column('cache_write_tokens')

    with op.batch_alter_table('usage_event', schema=None) as batch_op:
        batch_op.drop_column('image_count')
        batch_op.drop_column('cache_read_tokens')
        batch_op.drop_column('cache_write_tokens')

    with op.batch_alter_table('model', schema=None) as batch_op:
        batch_op.drop_column('image_prices')
        batch_op.drop_column('image_cost_per_image')
        batch_op.drop_column('cache_read_cost_per_token')
        batch_op.drop_column('cache_write_cost_per_token')

def data_upgrades() -> None:
    """Add any optional data upgrade migrations here!"""

def data_downgrades() -> None:
    """Add any optional data downgrade migrations here!"""
