"""Postgres rehearsal for the float→NUMERIC money migration (Plan 13 Phase 2).

The migration (``e5f6a7b8c9d0``) converts every money column from ``double
precision`` to ``NUMERIC(24, 12)`` with ``USING col::numeric(24, 12)``. These
tests prove the two properties that conversion relies on, exercised against the
*configured* backend (Postgres in CI's `just test-postgres`, SQLite locally):

* Postgres rounds each stored double to the operator's intended decimal at
  scale 12 — a float ``0.1`` (exact binary ``0.1000000000000000055…``) becomes
  exactly ``Decimal("0.100000000000")``, with no leaked binary tail.
* a ``SUM`` over the resulting NUMERIC column is exact and order-independent —
  ``0.1 + 0.2 + 0.3`` sums to exactly ``0.6``, where the same doubles summed as
  ``double precision`` drift to ``0.6000000000000001``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy import Column, Integer, MetaData, Numeric, Table, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine


@pytest.fixture
async def conn(database_url: str) -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        yield connection
    await engine.dispose()


def _is_postgres(conn: AsyncConnection) -> bool:
    return conn.engine.dialect.name == "postgresql"


# Known money values, including ones whose binary-float form has a nonzero tail
# past scale 12; each must land on its intended decimal after the cast.
_CASES = [
    ("0.1", Decimal("0.100000000000")),
    ("0.2", Decimal("0.200000000000")),
    ("0.3", Decimal("0.300000000000")),
    ("0.0000025", Decimal("0.000002500000")),
    ("0.00000010003", Decimal("0.000000100030")),
    ("123.456789", Decimal("123.456789000000")),
]


async def test_postgres_float_to_numeric_cast_recovers_intended_decimals(
    conn: AsyncConnection,
) -> None:
    if not _is_postgres(conn):
        pytest.skip("float→numeric cast semantics are Postgres-specific")
    await conn.execute(text("CREATE TEMP TABLE _probe (id int, v double precision)"))
    for i, (literal, _expected) in enumerate(_CASES):
        await conn.execute(
            text("INSERT INTO _probe (id, v) VALUES (:i, :v)"), {"i": i, "v": float(literal)}
        )
    # The exact conversion the migration performs.
    await conn.execute(
        text("ALTER TABLE _probe ALTER COLUMN v TYPE numeric(24, 12) USING v::numeric(24, 12)")
    )
    rows = (await conn.execute(text("SELECT id, v FROM _probe ORDER BY id"))).all()
    got = {row[0]: row[1] for row in rows}
    for i, (_literal, expected) in enumerate(_CASES):
        assert isinstance(got[i], Decimal)
        assert got[i] == expected
        assert str(got[i]) == str(expected)  # byte-identical scale, no binary tail


async def test_numeric_sum_is_exact_and_order_independent(conn: AsyncConnection) -> None:
    # Exercises the real app path: a SQLAlchemy ``Numeric(24, 12)`` column (the
    # `NUMERIC` money columns the migration produces) on whichever backend is
    # configured. SQLAlchemy's SQLite ``Numeric`` and Postgres ``NUMERIC`` both
    # store and ``SUM`` exactly — summing the same values as ``double`` drifts
    # (0.1 + 0.2 + 0.3 == 0.6000000000000001 in binary float).
    meta = MetaData()
    probe = Table(
        "_num_probe",
        meta,
        Column("id", Integer, primary_key=True),
        Column("v", Numeric(24, 12)),
    )
    await conn.run_sync(meta.create_all)
    # Insert the same multiset in two different orders; the SUM of each must be
    # byte-identical and exactly 0.6 — the order-independence guarantee, at the
    # DB layer this time (the pure-Python proof lives in test_decimal_money.py).
    forward_vals = [Decimal("0.1"), Decimal("0.2"), Decimal("0.3")]
    reverse_vals = list(reversed(forward_vals))
    await conn.execute(insert(probe), [{"id": i, "v": v} for i, v in enumerate(forward_vals)])
    forward = (await conn.execute(select(func.sum(probe.c.v)))).scalar()

    await conn.execute(text("DELETE FROM _num_probe"))
    await conn.execute(insert(probe), [{"id": i, "v": v} for i, v in enumerate(reverse_vals)])
    reverse = (await conn.execute(select(func.sum(probe.c.v)))).scalar()

    assert isinstance(forward, Decimal) and isinstance(reverse, Decimal)
    assert forward == reverse == Decimal("0.6")
    assert str(forward) == str(reverse)  # byte-identical, not merely equal
