"""SQLAlchemy column type for monetary amounts.

`NUMERIC(20, 6)` on PostgreSQL: exact storage, exact `SUM`, which is what the
budget gate reads. SQLite has no numeric affinity, so SQLAlchemy stores the value
as a float there and hands it back with binary noise; the type quantizes on the
way out so both dialects return the same scale and the domain never sees a
2.9999999999999996. SQLite is a development and test dialect only — production
refuses it (`config.py`) — so best-effort there is the deliberate position, and
the round-trip is asserted for both dialects rather than assumed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import Numeric
from sqlalchemy.types import TypeDecorator

from litestar_gateway.domain.money import COST_SCALE, quantize_cost

# 20 digits with 6 after the point: a ten-thousandth of a cent, and room for
# any plausible aggregate.
MONEY_PRECISION = 20
MONEY_SCALE = -int(COST_SCALE.as_tuple().exponent)  # 6


class Money(TypeDecorator):
    """A monetary amount, always a `Decimal` in Python."""

    impl = Numeric(MONEY_PRECISION, MONEY_SCALE, asdecimal=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        # A float from an un-migrated caller is converted via its decimal
        # string, not its binary expansion: `Decimal(0.1)` is not one tenth.
        return quantize_cost(value if isinstance(value, Decimal) else Decimal(str(value)))

    def process_result_value(self, value: Any, dialect: Any) -> Decimal | None:
        if value is None:
            return None
        return quantize_cost(value if isinstance(value, Decimal) else Decimal(str(value)))
