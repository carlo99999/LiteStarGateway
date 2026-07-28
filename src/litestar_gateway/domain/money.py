"""Money: one scale, one rounding rule, one place.

Every authoritative monetary value in the gateway is a `Decimal`. Binary floats
cannot represent ordinary decimal amounts exactly, so a ledger built on them
drifts with the number of rows it sums and gives an order-dependent total — for
a budget comparison, that is the difference between admitting a request and
refusing it. R3-L15 tracked this trade-off from Round 3; this module retires it.

Two scales, because the quantities differ by orders of magnitude:

- **rates** are per-token prices like `0.0000005`, so they keep 10 decimal
  places. Quantizing them at cost scale would round most real prices to zero;
- **costs** — a computed charge, a budget limit, an aggregate — keep 6, which is
  a ten-thousandth of a cent and finer than any provider bills.

Rounding is `ROUND_HALF_UP`, applied once, where a computed cost is produced.
Rounding at every intermediate step would accumulate the very bias the Decimal
migration exists to remove.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# 10 decimal places: per-token prices run to 7-8 significant decimals, and a
# rate is a multiplicand — precision lost here is multiplied by the token count.
RATE_SCALE = Decimal("0.0000000001")

# 6 decimal places: a ten-thousandth of a cent. Costs, limits and aggregates.
COST_SCALE = Decimal("0.000001")

ZERO = Decimal("0")


def to_rate(value: float | int | str | Decimal | None) -> Decimal | None:
    """A configured price as an exact `Decimal`, or `None` for an unpriced
    dimension.

    `str(value)` first when the input is a float: `Decimal(0.1)` captures the
    binary approximation (`0.1000000000000000055...`) while `Decimal("0.1")` is
    exactly one tenth. Values arriving from JSON or from a `float` column are
    meant as the decimal number the operator typed, so that is what they become.
    """
    if value is None:
        return None
    return _exact(value).quantize(RATE_SCALE, rounding=ROUND_HALF_UP)


def to_cost(value: float | int | str | Decimal) -> Decimal:
    """A monetary amount as an exact `Decimal` at cost scale."""
    return _exact(value).quantize(COST_SCALE, rounding=ROUND_HALF_UP)


def quantize_cost(value: Decimal) -> Decimal:
    """Round a computed amount to cost scale — once, at the end of a
    calculation, never between its terms."""
    return value.quantize(COST_SCALE, rounding=ROUND_HALF_UP)


def as_float(value: Decimal | None) -> float | None:
    """For the API edge only.

    Responses stay JSON numbers so the OpenAPI schema and the console are
    unaffected; `Decimal` remains authoritative in the domain and at rest. At
    cost scale a float round-trips faithfully enough to display, and no decision
    is ever taken on the result of this function.
    """
    return None if value is None else float(value)


def _exact(value: float | int | str | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))
