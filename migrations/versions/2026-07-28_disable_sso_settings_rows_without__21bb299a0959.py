"""disable sso settings rows without redirect uri

Revision ID: 21bb299a0959
Revises: d1054688b7c6
Create Date: 2026-07-28 11:19:09.664136

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
revision = '21bb299a0959'
down_revision = 'd1054688b7c6'
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
    """No schema change: this revision only neutralises unsafe data."""

def schema_downgrades() -> None:
    """No schema change to undo."""

def data_upgrades() -> None:
    """ISSUE-032: disable any enabled SSO configuration with no callback URL.

    With `redirect_uri` NULL the login flow derives the callback from the
    request's `Host`, so a forged host steers the redirect URI declared in the
    authorization request. The write path refuses to create such a row and the
    resolver refuses to use one outside local, but a row created before those
    checks would stay in the database claiming to be enabled while login fails
    — a persisted state that lies to the console.

    Unconditional on purpose: it is one row, and an environment-dependent data
    migration would make the resulting state depend on where it was run. In
    local development, where a derived callback is legal, re-enabling is one
    action in the console.
    """
    op.execute(
        "UPDATE sso_settings SET enabled = false "
        "WHERE enabled = true AND (redirect_uri IS NULL OR trim(redirect_uri) = '')"
    )

def data_downgrades() -> None:
    """Not reversible: which rows were disabled is not recorded, and
    re-enabling a configuration without a callback would restore the hole."""
