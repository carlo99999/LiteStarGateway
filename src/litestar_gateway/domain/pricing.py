"""The one normalized pricing function shared by reservation and settlement.

Plan 13 Phase 1. Both the pre-call reservation (an upper-bound estimate) and the
authoritative settlement compute cost through :func:`compute_cost`, given a
normalized :class:`BillableUsage` and a model's :class:`RateCard`. Routing both
paths through the same calculation is what guarantees the reservation and the
final charge can never disagree about how a given usage shape maps to a cost.

Phase 2 (Decimal migration, done) made the rate/cost fields and the arithmetic
here fixed-precision ``Decimal`` (see :mod:`litestar_gateway.domain.money`)
WITHOUT changing this module's shape: ``RateCard`` / ``BillableUsage`` /
``compute_cost`` stay the single seam where money is calculated. Token/image
*counts* remain ``int``; only the per-unit rates and the computed cost are money.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from litestar_gateway.domain.money import ZERO_MONEY, money


def image_price_key(size: str | None, quality: str | None) -> str | None:
    """Lookup key for a size/quality-specific image price, or ``None`` when either
    dimension is unknown (only the flat per-image fallback applies then)."""
    if size is None or quality is None:
        return None
    return f"{size}/{quality}"


@dataclass(frozen=True)
class RateCard:
    """The pricing inputs for one model.

    Every rate is a money ``Decimal`` (see :mod:`litestar_gateway.domain.money`)
    or ``None``. A ``None`` rate prices its dimension at zero — preserving the
    pre-Plan-13 behavior where Anthropic cache tokens and images were billed as
    zero until an operator configures an explicit rate. That makes every new
    dimension strictly opt-in: a model with no cache/image rates bills exactly as
    it did before.
    """

    input_cost_per_token: Decimal | None = None
    output_cost_per_token: Decimal | None = None
    # Anthropic prompt caching: distinct economics from ordinary input tokens
    # (design §1 — "do not fold them into ordinary input tokens"). Unset ⇒ 0.
    cache_write_cost_per_token: Decimal | None = None
    cache_read_cost_per_token: Decimal | None = None
    # Flat per-image price — the "simple per-call fallback" (design §1) applied
    # when no size/quality-specific entry in `image_prices` matches.
    image_cost_per_image: Decimal | None = None
    # Optional size/quality-specific per-image prices, keyed by `image_price_key`.
    image_prices: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class BillableUsage:
    """Normalized billable quantities of one call.

    Reservation fills this with upper-bound estimates (requested output ceiling,
    requested image count); settlement fills it with the authoritative counts the
    provider actually reported. Every field defaults to a no-cost zero, so a
    caller only sets the dimensions that apply to its operation.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    image_count: int = 0
    image_size: str | None = None
    image_quality: str | None = None


def image_unit_price(rates: RateCard, size: str | None, quality: str | None) -> Decimal:
    """Per-image price for the given dimensions: a size/quality-specific entry
    when configured, otherwise the flat per-image fallback, otherwise zero."""
    key = image_price_key(size, quality)
    if key is not None and key in rates.image_prices:
        return rates.image_prices[key]
    return rates.image_cost_per_image or ZERO_MONEY


def compute_cost(usage: BillableUsage, rates: RateCard) -> Decimal:
    """The single normalized usage→cost calculation (design §1).

    Cost is additive across independent dimensions, each priced at its own rate:
    ordinary input/output tokens, Anthropic cache-write/cache-read tokens, and
    generated images. A dimension a model does not price contributes zero, so a
    token-only call on a model with no image/cache rates costs exactly what it
    did before Plan 13 (the strict billing-regression guarantee).

    Exact ``Decimal`` (Plan 13 Phase 2): each term is an integer count times a
    money-scale rate, so the sum is exact at the money scale; the final
    :func:`money` quantization is a no-op for every legitimate rate (see
    :mod:`litestar_gateway.domain.money`) and only guards a pathological input.
    """
    return money(
        usage.prompt_tokens * (rates.input_cost_per_token or ZERO_MONEY)
        + usage.completion_tokens * (rates.output_cost_per_token or ZERO_MONEY)
        + usage.cache_write_tokens * (rates.cache_write_cost_per_token or ZERO_MONEY)
        + usage.cache_read_tokens * (rates.cache_read_cost_per_token or ZERO_MONEY)
        + usage.image_count * image_unit_price(rates, usage.image_size, usage.image_quality)
    )
