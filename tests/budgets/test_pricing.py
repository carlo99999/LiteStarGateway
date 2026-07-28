"""Table-driven fixture tests for the one normalized pricing function.

Plan 13 Phase 1, design §1. These pin exact dollar figures for every billable
dimension — ordinary tokens, Anthropic cache-write/cache-read tokens, and image
count/size/quality — so a change to the money math can't drift a known fixture
without failing here first. The same `compute_cost` is what both the pre-call
reservation and the authoritative settlement call.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from litestar_gateway.domain.exceptions import InvalidModelPricing
from litestar_gateway.domain.money import ZERO, to_cost, to_rate
from litestar_gateway.domain.pricing import (
    BillableUsage,
    RateCard,
    compute_cost,
    image_price_key,
    image_unit_price,
    rate_card,
    validate_rate_card,
)

RATE_FIELDS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_write_cost_per_token",
    "cache_read_cost_per_token",
    "image_cost_per_image",
)

# ── Token pricing (regression: byte-identical to the pre-Plan-13 formula) ─────


@pytest.mark.parametrize(
    ("prompt", "completion", "input_rate", "output_rate", "expected"),
    [
        (0, 0, "0.001", "0.002", "0.000000"),
        (1000, 0, "0.001", "0.002", "1.000000"),
        (0, 500, "0.001", "0.002", "1.000000"),
        (1000, 500, "0.001", "0.002", "2.000000"),
        # 100 x 0.00003 + 200 x 0.00006 — exact in decimal, 0.015000000000000001
        # in binary floats. The point of the migration in one row.
        (100, 200, "0.00003", "0.00006", "0.015000"),
    ],
)
def test_token_only_cost_matches_prompt_input_plus_completion_output(
    prompt: int, completion: int, input_rate: str, output_rate: str, expected: str
) -> None:
    usage = BillableUsage(prompt_tokens=prompt, completion_tokens=completion)
    rates = rate_card(input_cost_per_token=input_rate, output_cost_per_token=output_rate)
    # Exact equality, not approx: that is what Decimal buys.
    assert compute_cost(usage, rates) == Decimal(expected)


def test_unpriced_model_bills_zero_for_tokens() -> None:
    usage = BillableUsage(prompt_tokens=1000, completion_tokens=1000)
    assert compute_cost(usage, rate_card()) == ZERO


# ── Anthropic cache tokens: their own rates, never folded into input ──────────


def test_cache_tokens_priced_at_their_own_rates() -> None:
    # design §1: cache-write and cache-read have distinct economics from input.
    usage = BillableUsage(
        prompt_tokens=100,
        completion_tokens=50,
        cache_write_tokens=200,
        cache_read_tokens=400,
    )
    rates = rate_card(
        input_cost_per_token=0.001,
        output_cost_per_token=0.002,
        cache_write_cost_per_token=0.00125,  # ~1.25x input (Anthropic economics)
        cache_read_cost_per_token=0.0001,  # ~0.1x input
    )
    expected = 100 * 0.001 + 50 * 0.002 + 200 * 0.00125 + 400 * 0.0001
    assert compute_cost(usage, rates) == to_cost(expected)


def test_cache_tokens_unbilled_when_no_cache_rates_configured() -> None:
    # Strictly opt-in: surfacing cache tokens must not change the bill until an
    # operator configures cache rates (preserves pre-Plan-13 behavior).
    usage = BillableUsage(prompt_tokens=100, cache_write_tokens=999, cache_read_tokens=999)
    rates = rate_card(input_cost_per_token=0.001)
    assert compute_cost(usage, rates) == to_cost(0.1)


# ── Image pricing: count/size/quality with a flat per-call fallback ───────────


def test_three_images_at_flat_per_image_rate() -> None:
    # "3 images at flat rate 0.04 = 0.12 exactly."
    usage = BillableUsage(image_count=3, image_size="1024x1024", image_quality="standard")
    rates = rate_card(image_cost_per_image=0.04)
    assert compute_cost(usage, rates) == to_cost(0.12)


def test_image_price_key_format() -> None:
    assert image_price_key("1024x1024", "hd") == "1024x1024/hd"


def test_size_quality_specific_price_overrides_flat_fallback() -> None:
    usage = BillableUsage(image_count=2, image_size="1024x1024", image_quality="hd")
    rates = rate_card(
        image_cost_per_image=0.04,
        image_prices={"1024x1024/hd": 0.08, "1024x1024/standard": 0.04},
    )
    assert compute_cost(usage, rates) == to_cost(0.16)  # 2 * 0.08


def test_unmatched_size_quality_falls_back_to_flat_rate() -> None:
    usage = BillableUsage(image_count=1, image_size="512x512", image_quality="standard")
    rates = rate_card(image_cost_per_image=0.02, image_prices={"1024x1024/hd": 0.08})
    assert compute_cost(usage, rates) == to_cost(0.02)


def test_image_without_any_rate_bills_zero() -> None:
    usage = BillableUsage(image_count=5, image_size="1024x1024", image_quality="standard")
    assert compute_cost(usage, rate_card()) == ZERO


def test_missing_dimension_uses_flat_fallback() -> None:
    # size or quality None ⇒ no composite key ⇒ flat fallback.
    assert image_price_key(None, "standard") is None
    assert image_price_key("1024x1024", None) is None
    usage = BillableUsage(image_count=2, image_size=None, image_quality=None)
    rates = rate_card(
        image_cost_per_image=0.03,
        image_prices={"1024x1024/standard": 0.05},
    )
    assert compute_cost(usage, rates) == to_cost(0.06)


def test_image_unit_price_helper() -> None:
    rates = rate_card(
        image_cost_per_image=0.01,
        image_prices={"1024x1024/hd": 0.09},
    )
    assert image_unit_price(rates, "1024x1024", "hd") == to_rate(0.09)
    assert image_unit_price(rates, "512x512", "standard") == to_rate(0.01)
    assert image_unit_price(rate_card(), "512x512", "standard") == ZERO


# ── All dimensions combine additively ─────────────────────────────────────────


def test_all_dimensions_sum_independently() -> None:
    usage = BillableUsage(
        prompt_tokens=10,
        completion_tokens=20,
        cache_write_tokens=30,
        cache_read_tokens=40,
        image_count=2,
        image_size="1024x1024",
        image_quality="standard",
    )
    rates = rate_card(
        input_cost_per_token=1.0,
        output_cost_per_token=2.0,
        cache_write_cost_per_token=3.0,
        cache_read_cost_per_token=4.0,
        image_cost_per_image=5.0,
    )
    expected = 10 * 1 + 20 * 2 + 30 * 3 + 40 * 4 + 2 * 5
    assert compute_cost(usage, rates) == to_cost(expected)


# ── Rate validation: a rate is never negative or non-finite (ISSUE-022) ───────


@pytest.mark.parametrize("field", RATE_FIELDS)
@pytest.mark.parametrize("bad", [-1.0, -0.000001, -5, float("nan"), float("inf"), float("-inf")])
def test_validate_rate_card_rejects_negative_or_non_finite_rate(field: str, bad: float) -> None:
    # Every dimension, not just tokens: a negative rate makes `compute_cost`
    # return a credit, which the ledger and the budget gate both trust.
    rates: dict[str, Any] = {field: bad}
    with pytest.raises(InvalidModelPricing, match=field):
        validate_rate_card(RateCard(**rates))


@pytest.mark.parametrize("field", RATE_FIELDS)
def test_validate_rate_card_accepts_zero_and_none(field: str) -> None:
    zero: dict[str, Any] = {field: 0.0}
    unset: dict[str, Any] = {field: None}
    validate_rate_card(RateCard(**zero))
    validate_rate_card(RateCard(**unset))


def test_validate_rate_card_accepts_ordinary_positive_rates() -> None:
    validate_rate_card(
        RateCard(
            input_cost_per_token=0.000005,  # type: ignore[arg-type]
            output_cost_per_token=0.000015,  # type: ignore[arg-type]
            cache_write_cost_per_token=0.00000625,  # type: ignore[arg-type]
            cache_read_cost_per_token=0.0000005,  # type: ignore[arg-type]
            image_cost_per_image=0.04,  # type: ignore[arg-type]
            image_prices={"1024x1024/hd": 0.08, "1024x1024/standard": 0.0},  # type: ignore[arg-type]
        )
    )


@pytest.mark.parametrize("bad", [-0.01, float("nan"), float("inf")])
def test_validate_rate_card_rejects_bad_image_price_entry(bad: float) -> None:
    with pytest.raises(InvalidModelPricing, match="1024x1024/hd"):
        validate_rate_card(RateCard(image_prices={"1024x1024/hd": bad}))  # type: ignore[dict-item]


@pytest.mark.parametrize("bad", ["0.01", None, True, [], {}])
def test_validate_rate_card_rejects_non_numeric_rate(bad: object) -> None:
    # `None` means "unpriced" on a scalar rate, but an explicit entry in
    # `image_prices` must be a real number — a JSON body reaches here untyped.
    with pytest.raises(InvalidModelPricing):
        validate_rate_card(RateCard(image_prices={"1024x1024/hd": bad}))  # type: ignore[dict-item]  # type: ignore[dict-item]


def test_validated_rate_card_can_never_produce_a_credit() -> None:
    # The invariant the validation exists to protect: cost is never negative,
    # so settlement can only ever debit the ledger.
    configured = RateCard(  # raw, as the operator typed them
        input_cost_per_token=0.001,  # type: ignore[arg-type]
        output_cost_per_token=0.0,  # type: ignore[arg-type]
        image_cost_per_image=0.04,  # type: ignore[arg-type]
        image_prices={"1024x1024/hd": 0.0},  # type: ignore[arg-type]
    )
    validate_rate_card(configured)
    # Validation first, conversion second — the order production uses.
    rates = rate_card(
        input_cost_per_token=0.001,
        output_cost_per_token=0.0,
        image_cost_per_image=0.04,
        image_prices={"1024x1024/hd": 0.0},
    )
    usage = BillableUsage(
        prompt_tokens=7,
        completion_tokens=3,
        image_count=1,
        image_size="1024x1024",
        image_quality="hd",
    )
    assert compute_cost(usage, rates) >= ZERO
