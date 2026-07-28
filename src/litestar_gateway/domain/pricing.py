"""The one normalized pricing function shared by reservation and settlement.

Plan 13 Phase 1. Both the pre-call reservation (an upper-bound estimate) and the
authoritative settlement compute cost through :func:`compute_cost`, given a
normalized :class:`BillableUsage` and a model's :class:`RateCard`. Routing both
paths through the same calculation is what guarantees the reservation and the
final charge can never disagree about how a given usage shape maps to a cost.

Money is `Decimal` here (Plan 13 Phase 2, retiring R3-L15): rates and costs are
exact decimal quantities, with the scales and the single rounding step defined in
`domain/money.py`. Token counts stay `int` — they are counts, not money. The
module's shape is unchanged: ``RateCard`` / ``BillableUsage`` / ``compute_cost``
remain the single seam where money is calculated.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from litestar_gateway.domain.exceptions import InvalidModelPricing
from litestar_gateway.domain.money import ZERO, quantize_cost, to_rate


def image_price_key(size: str | None, quality: str | None) -> str | None:
    """Lookup key for a size/quality-specific image price, or ``None`` when either
    dimension is unknown (only the flat per-image fallback applies then)."""
    if size is None or quality is None:
        return None
    return f"{size}/{quality}"


@dataclass(frozen=True)
class RateCard:
    """The pricing inputs for one model.

    A ``None`` rate prices its dimension at ``0.0`` — preserving the pre-Plan-13
    behavior where Anthropic cache tokens and images were billed as zero until an
    operator configures an explicit rate. That makes every new dimension strictly
    opt-in: a model with no cache/image rates bills exactly as it did before.
    """

    input_cost_per_token: Decimal | None = None
    output_cost_per_token: Decimal | None = None
    # Anthropic prompt caching: distinct economics from ordinary input tokens
    # (design §1 — "do not fold them into ordinary input tokens"). Unset ⇒ 0.0.
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


_RATE_FIELDS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_write_cost_per_token",
    "cache_read_cost_per_token",
    "image_cost_per_image",
)


def _validate_rate(name: str, value: object, *, optional: bool = True) -> None:
    if value is None and optional:
        return  # unpriced dimension — bills at 0.0
    # A string is refused even though `Decimal("0.01")` would parse: rates reach
    # this from JSON bodies, where a quoted number means the client sent the
    # wrong type, and silently accepting it would hide that.
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise InvalidModelPricing(f"{name} must be a number, got {value!r}")
    try:
        exact = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise InvalidModelPricing(f"{name} must be a number, got {value!r}") from exc
    if not exact.is_finite():
        raise InvalidModelPricing(f"{name} must be a finite number, got {value!r}")
    if exact < 0:
        raise InvalidModelPricing(f"{name} must be zero or positive, got {value}")


def validate_rate_card(rates: RateCard) -> None:
    """Reject any rate that would make :func:`compute_cost` return a credit.

    `compute_cost` multiplies these rates directly and the result is persisted
    in the same ledger the budget gate reads, so a negative (or NaN/inf) rate is
    not a display problem: it hands the team spend back and defeats a hard cap.
    Every write path that can set a rate must call this — enforcement lives here,
    in the domain, rather than in one controller, so a new caller inherits it.
    `None` stays legal (the dimension is simply unpriced) and so does an explicit
    zero.
    """
    for name in _RATE_FIELDS:
        _validate_rate(name, getattr(rates, name))
    for key, value in rates.image_prices.items():
        # An explicit entry must be a real number: `image_unit_price` returns it
        # verbatim, so a `null` from a JSON body would blow up settlement's
        # arithmetic rather than fall back to the flat per-image rate.
        _validate_rate(f"image_prices[{key}]", value, optional=False)


def image_unit_price(rates: RateCard, size: str | None, quality: str | None) -> Decimal:
    """Per-image price for the given dimensions: a size/quality-specific entry
    when configured, otherwise the flat per-image fallback, otherwise zero."""
    key = image_price_key(size, quality)
    if key is not None and key in rates.image_prices:
        return rates.image_prices[key]
    return rates.image_cost_per_image or ZERO


def compute_cost(usage: BillableUsage, rates: RateCard) -> Decimal:
    """The single normalized usage→cost calculation (design §1).

    Cost is additive across independent dimensions, each priced at its own rate:
    ordinary input/output tokens, Anthropic cache-write/cache-read tokens, and
    generated images. A dimension a model does not price contributes ``0.0``, so
    a token-only call on a model with no image/cache rates costs exactly what it
    did before Plan 13 (the strict billing-regression guarantee).
    """
    # Rounded ONCE, here, after every term is summed exactly: quantizing each
    # product would bias the total in the direction of the rounding rule.
    return quantize_cost(
        usage.prompt_tokens * (rates.input_cost_per_token or ZERO)
        + usage.completion_tokens * (rates.output_cost_per_token or ZERO)
        + usage.cache_write_tokens * (rates.cache_write_cost_per_token or ZERO)
        + usage.cache_read_tokens * (rates.cache_read_cost_per_token or ZERO)
        + usage.image_count * image_unit_price(rates, usage.image_size, usage.image_quality)
    )


def rate_card(
    *,
    input_cost_per_token: float | str | Decimal | None = None,
    output_cost_per_token: float | str | Decimal | None = None,
    cache_write_cost_per_token: float | str | Decimal | None = None,
    cache_read_cost_per_token: float | str | Decimal | None = None,
    image_cost_per_image: float | str | Decimal | None = None,
    image_prices: Mapping[str, float | str | Decimal] | None = None,
) -> RateCard:
    """Build a `RateCard` from values that may still be floats.

    The single conversion point for rates crossing in from JSON bodies or from
    columns not yet migrated — so `Decimal(str(x))` happens once, here, rather
    than being re-derived (and mis-derived) at each call site.
    """
    return RateCard(
        input_cost_per_token=to_rate(input_cost_per_token),
        output_cost_per_token=to_rate(output_cost_per_token),
        cache_write_cost_per_token=to_rate(cache_write_cost_per_token),
        cache_read_cost_per_token=to_rate(cache_read_cost_per_token),
        image_cost_per_image=to_rate(image_cost_per_image),
        image_prices={
            key: converted
            for key, value in (image_prices or {}).items()
            if (converted := to_rate(value)) is not None
        },
    )
