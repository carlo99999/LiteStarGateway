"""Canonical money representation for the gateway (Plan 13 Phase 2).

Every authoritative monetary value — per-token / per-image rates, computed
costs, budget limits, in-flight reservations, aggregated spend — is a
fixed-precision ``Decimal`` produced through this module, never a binary
``float``. Routing all money through one scale and one rounding mode is what
makes repeated aggregation order-independent and keeps binary-float drift out
of budget comparisons (Plan 13 Phase 2, "Done when: repeated aggregation is
order-independent and no binary-float drift reaches budget comparisons").

**Scale — 12 fractional digits.** The most precise rates configured anywhere in
this codebase today live in the bundled price catalog (``model_prices.json``):
per-token rates down to ~``1e-7`` (e.g. ``0.00000010003``, 11 places). 12 places
represents every such rate exactly, with headroom. And the scale never needs to
grow past that: every money operation in this codebase — a rate times an integer
token/image count, a sum of costs, a savings *delta* (rate minus rate, times a
count) — preserves or *shrinks* the number of fractional digits; none creates
new ones (there is no division of money). So a cost, a spend total, or a savings
figure never has more fractional digits than the rates it came from, and
quantizing to 12 places is a no-op for every legitimate value. Rounding only
ever engages for a pathological input carrying more than 12 places, never to
silently truncate a real charge.

**Rounding — banker's rounding (``ROUND_HALF_EVEN``),** the standard tie-break
for financial accumulation: unbiased over many roundings, unlike round-half-up.

**Decimal-from-string discipline.** Build money from a string or integer, NEVER
from a float literal — ``Decimal(0.01)`` is
``Decimal('0.01000000000000000020816681711721685...')`` whereas
``Decimal("0.01")`` is exact. :func:`money` funnels a stray float through
``str()`` (whose shortest round-trippable repr recovers the intended decimal for
any human-entered rate, e.g. ``str(0.0000025) == '2.5e-06'``) so a boundary that
still hands us a float — a JSON body decoded to ``float``, a legacy ``float``
column read before its migration — cannot re-introduce the very imprecision
this migration exists to eliminate.

The DB backs every money column with ``NUMERIC(MONEY_PRECISION, MONEY_SCALE)``;
Postgres ``NUMERIC`` and SQLAlchemy's SQLite ``Numeric`` both store and ``SUM``
these exactly, so DB-side aggregation carries no float drift either.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

# Fractional digits every rate/cost/limit is carried at. See module docstring.
MONEY_SCALE = 12
# Tie-break for the (no-op-in-practice) final quantization. See module docstring.
MONEY_ROUNDING = ROUND_HALF_EVEN
# DB ``NUMERIC(precision, scale)``: 12 integer digits (budgets up to ~1e12 USD)
# plus the 12 fractional digits above. Comfortably inside Decimal's default
# 28-digit context, so quantization of any in-range value never raises.
MONEY_PRECISION = 24

# Decimal("1E-12") — the quantization target for MONEY_SCALE places.
_QUANTUM = Decimal(1).scaleb(-MONEY_SCALE)

# Canonical zero at the money scale — the additive identity for reservations,
# spend and cost, and the value a priced-at-zero dimension contributes.
ZERO_MONEY = Decimal(0).quantize(_QUANTUM, rounding=MONEY_ROUNDING)


def money(value: str | int | Decimal | float) -> Decimal:
    """Normalize a value to the canonical money ``Decimal`` (scale 12, banker's
    rounding).

    Accepts ``str`` / ``int`` / ``Decimal`` directly. A ``float`` is coerced via
    ``str()`` rather than the imprecise ``Decimal(float)`` constructor (see the
    module docstring on Decimal-from-string discipline), so a value that still
    arrives as a float at a boundary is recovered as its intended decimal
    instead of its binary expansion.
    """
    if isinstance(value, float):
        value = str(value)
    return Decimal(value).quantize(_QUANTUM, rounding=MONEY_ROUNDING)
