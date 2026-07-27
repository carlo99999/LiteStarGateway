"""decimalize money columns to numeric

Revision ID: e5f6a7b8c9d0
Revises: 47e59bf43231
Create Date: 2026-07-27 12:00:00.000000

Plan 13 Phase 2 (Decimal money): convert every authoritative monetary column
from binary ``Float`` (Postgres ``double precision``) to fixed-scale
``NUMERIC(MONEY_PRECISION, MONEY_SCALE)`` = ``NUMERIC(24, 12)`` so DB-side
``SUM`` is order-independent and carries no binary-float drift into the budget
gate (domain.money).

Autogenerate does not emit this: SQLite has no distinct ``Float`` vs ``Numeric``
storage type, so a fresh-SQLite drift check sees no diff. The change is
nonetheless required on Postgres, where ``double precision`` and ``numeric`` are
genuinely different types with different aggregation semantics — hence this
hand-written revision.

Existing-row conversion (Postgres): ``ALTER COLUMN ... TYPE numeric(24,12)
USING col::numeric(24,12)``. Postgres rounds each stored double to the nearest
value representable at scale 12, which recovers the operator's intended decimal
for every rate/cost the gateway stores (e.g. a float ``0.1``, whose exact binary
value is ``0.1000000000000000055...``, becomes exactly ``0.100000000000``).
Rehearsed on Postgres via ``just test-postgres`` and
``tests/misc/test_decimal_money_migration.py``.

On SQLite the batch recreate copies values as-is into the new NUMERIC-affinity
column; test databases are populated through the ORM after migration, so they
round-trip as exact Decimals regardless.
"""

import warnings
from typing import TYPE_CHECKING

import sqlalchemy as sa
from advanced_alchemy.types import (
    GUID,
    ORA_JSONB,
    Bool,
    DateTimeUTC,
    EncryptedString,
    EncryptedText,
    FernetBackend,
    JsonB,
    OneTimeCode,
    PasswordHash,
    StoredObject,
    TOTPSecret,
)
from advanced_alchemy.types.encrypted_string import PGCryptoBackend
from advanced_alchemy.types.password_hash.argon2 import Argon2Hasher
from advanced_alchemy.types.password_hash.passlib import PasslibHasher
from advanced_alchemy.types.password_hash.pwdlib import PwdlibHasher
from alembic import op
from sqlalchemy import Text  # noqa: F401

if TYPE_CHECKING:
    pass

__all__ = (
    "data_downgrades",
    "data_upgrades",
    "downgrade",
    "schema_downgrades",
    "schema_upgrades",
    "upgrade",
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
revision = "e5f6a7b8c9d0"
down_revision = "47e59bf43231"
branch_labels = None
depends_on = None

# NUMERIC(MONEY_PRECISION, MONEY_SCALE) — kept in lockstep with domain.money.
_MONEY = sa.Numeric(24, 12)
_FLOAT = sa.Float()

# (table, column, nullable) for every authoritative money column.
_MONEY_COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("model", "input_cost_per_token", True),
    ("model", "output_cost_per_token", True),
    ("model", "cache_write_cost_per_token", True),
    ("model", "cache_read_cost_per_token", True),
    ("model", "image_cost_per_image", True),
    ("usage_event", "cost", False),
    ("pending_usage_event", "cost", False),
    ("team_budget", "limit_cost", False),
    ("pending_budget_alert", "spend", False),
    ("pending_budget_alert", "limit_cost", False),
    ("routing_decision", "chosen_input_cost", True),
    ("routing_decision", "chosen_output_cost", True),
    ("routing_decision", "alt_input_cost", True),
    ("routing_decision", "alt_output_cost", True),
)


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


def _alter(
    table: str,
    column: str,
    *,
    to_type: sa.types.TypeEngine,
    from_type: sa.types.TypeEngine,
    nullable: bool,
) -> None:
    # postgresql_using is a no-op under SQLite batch mode (table recreate) and
    # supplies the explicit float→numeric cast on Postgres.
    using = None
    if isinstance(to_type, sa.Numeric):
        using = f"{column}::numeric(24, 12)"
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.alter_column(
            column,
            existing_type=from_type,
            type_=to_type,
            existing_nullable=nullable,
            postgresql_using=using,
        )


def schema_upgrades() -> None:
    """schema upgrade migrations go here."""
    for table, column, nullable in _MONEY_COLUMNS:
        _alter(table, column, to_type=_MONEY, from_type=_FLOAT, nullable=nullable)


def schema_downgrades() -> None:
    """schema downgrade migrations go here."""
    for table, column, nullable in reversed(_MONEY_COLUMNS):
        _alter(table, column, to_type=_FLOAT, from_type=_MONEY, nullable=nullable)


def data_upgrades() -> None:
    """Add any optional data upgrade migrations here!"""


def data_downgrades() -> None:
    """Add any optional data downgrade migrations here!"""
