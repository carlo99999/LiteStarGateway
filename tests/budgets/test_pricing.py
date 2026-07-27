"""Table-driven fixture tests for the one normalized pricing function.

Plan 13 Phase 1, design §1. These pin exact dollar figures for every billable
dimension — ordinary tokens, Anthropic cache-write/cache-read tokens, and image
count/size/quality — so a change to the money math can't drift a known fixture
without failing here first. The same `compute_cost` is what both the pre-call
reservation and the authoritative settlement call.
"""

from __future__ import annotations

import pytest

from litestar_gateway.domain.pricing import (
    BillableUsage,
    RateCard,
    compute_cost,
    image_price_key,
    image_unit_price,
)

# ── Token pricing (regression: byte-identical to the pre-Plan-13 formula) ─────


@pytest.mark.parametrize(
    ("prompt", "completion", "input_rate", "output_rate", "expected"),
    [
        (0, 0, 0.001, 0.002, 0.0),
        (1000, 0, 0.001, 0.002, 1.0),
        (0, 500, 0.001, 0.002, 1.0),
        (1000, 500, 0.001, 0.002, 2.0),
        (100, 200, 0.00003, 0.00006, 100 * 0.00003 + 200 * 0.00006),
    ],
)
def test_token_only_cost_matches_prompt_input_plus_completion_output(
    prompt: int, completion: int, input_rate: float, output_rate: float, expected: float
) -> None:
    usage = BillableUsage(prompt_tokens=prompt, completion_tokens=completion)
    rates = RateCard(input_cost_per_token=input_rate, output_cost_per_token=output_rate)
    assert compute_cost(usage, rates) == pytest.approx(expected)


def test_unpriced_model_bills_zero_for_tokens() -> None:
    usage = BillableUsage(prompt_tokens=1000, completion_tokens=1000)
    assert compute_cost(usage, RateCard()) == 0.0


# ── Anthropic cache tokens: their own rates, never folded into input ──────────


def test_cache_tokens_priced_at_their_own_rates() -> None:
    # design §1: cache-write and cache-read have distinct economics from input.
    usage = BillableUsage(
        prompt_tokens=100,
        completion_tokens=50,
        cache_write_tokens=200,
        cache_read_tokens=400,
    )
    rates = RateCard(
        input_cost_per_token=0.001,
        output_cost_per_token=0.002,
        cache_write_cost_per_token=0.00125,  # ~1.25x input (Anthropic economics)
        cache_read_cost_per_token=0.0001,  # ~0.1x input
    )
    expected = 100 * 0.001 + 50 * 0.002 + 200 * 0.00125 + 400 * 0.0001
    assert compute_cost(usage, rates) == pytest.approx(expected)


def test_cache_tokens_unbilled_when_no_cache_rates_configured() -> None:
    # Strictly opt-in: surfacing cache tokens must not change the bill until an
    # operator configures cache rates (preserves pre-Plan-13 behavior).
    usage = BillableUsage(prompt_tokens=100, cache_write_tokens=999, cache_read_tokens=999)
    rates = RateCard(input_cost_per_token=0.001)
    assert compute_cost(usage, rates) == pytest.approx(0.1)


# ── Image pricing: count/size/quality with a flat per-call fallback ───────────


def test_three_images_at_flat_per_image_rate() -> None:
    # "3 images at flat rate 0.04 = 0.12 exactly."
    usage = BillableUsage(image_count=3, image_size="1024x1024", image_quality="standard")
    rates = RateCard(image_cost_per_image=0.04)
    assert compute_cost(usage, rates) == pytest.approx(0.12)


def test_size_quality_specific_price_overrides_flat_fallback() -> None:
    usage = BillableUsage(image_count=2, image_size="1024x1024", image_quality="hd")
    rates = RateCard(
        image_cost_per_image=0.04,
        image_prices={
            image_price_key("1024x1024", "hd"): 0.08,  # type: ignore[dict-item]
            image_price_key("1024x1024", "standard"): 0.04,  # type: ignore[dict-item]
        },
    )
    assert compute_cost(usage, rates) == pytest.approx(0.16)  # 2 * 0.08


def test_unmatched_size_quality_falls_back_to_flat_rate() -> None:
    usage = BillableUsage(image_count=1, image_size="512x512", image_quality="standard")
    rates = RateCard(
        image_cost_per_image=0.02,
        image_prices={image_price_key("1024x1024", "hd"): 0.08},  # type: ignore[dict-item]
    )
    assert compute_cost(usage, rates) == pytest.approx(0.02)


def test_image_without_any_rate_bills_zero() -> None:
    usage = BillableUsage(image_count=5, image_size="1024x1024", image_quality="standard")
    assert compute_cost(usage, RateCard()) == 0.0


def test_missing_dimension_uses_flat_fallback() -> None:
    # size or quality None ⇒ no composite key ⇒ flat fallback.
    assert image_price_key(None, "standard") is None
    assert image_price_key("1024x1024", None) is None
    usage = BillableUsage(image_count=2, image_size=None, image_quality=None)
    rates = RateCard(
        image_cost_per_image=0.03,
        image_prices={image_price_key("1024x1024", "standard"): 0.05},  # type: ignore[dict-item]
    )
    assert compute_cost(usage, rates) == pytest.approx(0.06)


def test_image_unit_price_helper() -> None:
    rates = RateCard(
        image_cost_per_image=0.01,
        image_prices={image_price_key("1024x1024", "hd"): 0.09},  # type: ignore[dict-item]
    )
    assert image_unit_price(rates, "1024x1024", "hd") == 0.09
    assert image_unit_price(rates, "512x512", "standard") == 0.01
    assert image_unit_price(RateCard(), "512x512", "standard") == 0.0


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
    rates = RateCard(
        input_cost_per_token=1.0,
        output_cost_per_token=2.0,
        cache_write_cost_per_token=3.0,
        cache_read_cost_per_token=4.0,
        image_cost_per_image=5.0,
    )
    expected = 10 * 1 + 20 * 2 + 30 * 3 + 40 * 4 + 2 * 5
    assert compute_cost(usage, rates) == pytest.approx(expected)
