"""add service_principal table + api_key.service_principal_id

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
Create Date: 2026-07-03 18:00:00.000000

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
revision = "e0f1a2b3c4d5"
down_revision = "d9e0f1a2b3c4"
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
        "service_principal",
        sa.Column("id", sa.GUID(length=16), nullable=False),
        sa.Column("sa_orm_sentinel", sa.Integer(), nullable=True),
        sa.Column("team_id", sa.GUID(length=16), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTimeUTC(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTimeUTC(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["team_id"], ["team.id"], name="fk_service_principal_team_id_team"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_service_principal"),
    )
    with op.batch_alter_table("service_principal", schema=None) as batch_op:
        batch_op.create_index("ix_service_principal_team_id", ["team_id"])
    # Existing keys stay personal (NULL service_principal_id).
    with op.batch_alter_table("api_key", schema=None) as batch_op:
        batch_op.add_column(sa.Column("service_principal_id", sa.GUID(length=16), nullable=True))
        batch_op.create_index(
            "ix_api_key_service_principal_id", ["service_principal_id"]
        )
        batch_op.create_foreign_key(
            "fk_api_key_service_principal_id_service_principal",
            "service_principal",
            ["service_principal_id"],
            ["id"],
        )


def schema_downgrades() -> None:
    """schema downgrade migrations go here."""
    with op.batch_alter_table("api_key", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_api_key_service_principal_id_service_principal", type_="foreignkey"
        )
        batch_op.drop_index("ix_api_key_service_principal_id")
        batch_op.drop_column("service_principal_id")
    with op.batch_alter_table("service_principal", schema=None) as batch_op:
        batch_op.drop_index("ix_service_principal_team_id")
    op.drop_table("service_principal")


def data_upgrades() -> None:
    """Add any optional data upgrade migrations here!"""


def data_downgrades() -> None:
    """Add any optional data downgrade migrations here!"""
