"""Decimal money migration (Plan 13 Phase 2).

Pins the money-correctness invariants the migration exists to guarantee:

* rates/costs are exact ``Decimal`` at a fixed scale, built from strings so no
  binary-float imprecision leaks in (``money(0.01) == Decimal("0.01")``);
* ``compute_cost`` returns an exact ``Decimal`` for known fixtures;
* repeated aggregation is **order-independent** — the same multiset of costs
  summed in any order is byte-identical (impossible to guarantee with float,
  the whole point of the migration);
* budget comparisons stay pure ``Decimal`` (a spend exactly equal to the limit
  is blocked, with no float/Decimal drift at the boundary).
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest

from litestar_gateway.application.usage_meter import InFlightSpend, UsageMeter
from litestar_gateway.domain.entities import Budget, BudgetWindow, Model, ModelType, Provider
from litestar_gateway.domain.exceptions import BudgetExceeded
from litestar_gateway.domain.money import (
    MONEY_ROUNDING,
    MONEY_SCALE,
    ZERO_MONEY,
    money,
)
from litestar_gateway.domain.ports import BudgetRepository, UsageRepository
from litestar_gateway.domain.pricing import BillableUsage, RateCard, compute_cost

# ── money(): construction and Decimal-from-string discipline ─────────────────


def test_money_from_string_is_exact() -> None:
    assert money("0.01") == Decimal("0.01")
    assert str(money("0.01")) == "0.010000000000"  # quantized to MONEY_SCALE


def test_money_from_float_recovers_intended_decimal_not_binary_expansion() -> None:
    # The single most common Decimal-migration bug: Decimal(0.01) is
    # 0.01000000000000000020816... — money() must route through str() so a
    # stray float still yields the intended decimal.
    assert money(0.01) == Decimal("0.01")
    assert money(0.0000025) == Decimal("0.0000025")
    assert money(0.01) != Decimal(0.01)  # the trap money() avoids


def test_money_scale_and_rounding_are_the_documented_choices() -> None:
    assert MONEY_SCALE == 12
    assert MONEY_ROUNDING is ROUND_HALF_EVEN
    assert ZERO_MONEY == Decimal(0)
    assert f"{ZERO_MONEY:.4f}" == "0.0000"  # formats cleanly despite 0E-12 repr


def test_money_uses_bankers_rounding_at_the_scale_boundary() -> None:
    # 13th-place ties round to the even 12th digit (ROUND_HALF_EVEN).
    assert money("0.0000000000005") == Decimal("0.000000000000")  # ties to even (0)
    assert money("0.0000000000015") == Decimal("0.000000000002")  # ties to even (2)


# ── compute_cost: exact Decimal for known fixtures ───────────────────────────


def test_compute_cost_is_exact_decimal() -> None:
    rates = RateCard(input_cost_per_token=money("0.01"), output_cost_per_token=money("0.03"))
    usage = BillableUsage(prompt_tokens=1, completion_tokens=1)
    cost = compute_cost(usage, rates)
    assert isinstance(cost, Decimal)
    # 0.01 + 0.03 == exactly 0.04 — the classic float sum that drifts to
    # 0.04000000000000001 stays exact here.
    assert cost == Decimal("0.04")
    assert str(cost) == "0.040000000000"


def test_compute_cost_none_rate_prices_dimension_at_exact_zero() -> None:
    rates = RateCard(input_cost_per_token=money("0.01"))  # no output rate
    usage = BillableUsage(prompt_tokens=10, completion_tokens=999)
    assert compute_cost(usage, rates) == Decimal("0.1")  # completion contributes nothing


def test_compute_cost_sums_all_dimensions_exactly() -> None:
    rates = RateCard(
        input_cost_per_token=money("0.001"),
        output_cost_per_token=money("0.002"),
        cache_write_cost_per_token=money("0.003"),
        cache_read_cost_per_token=money("0.004"),
        image_cost_per_image=money("0.05"),
    )
    usage = BillableUsage(
        prompt_tokens=100,
        completion_tokens=100,
        cache_write_tokens=100,
        cache_read_tokens=100,
        image_count=2,
    )
    # 0.1 + 0.2 + 0.3 + 0.4 + 0.10 == exactly 1.10
    assert compute_cost(usage, rates) == Decimal("1.10")


# ── rate parsing safety: 0.01 round-trips to exactly Decimal("0.01") ─────────


def test_rate_parsed_via_money_has_exact_string_representation() -> None:
    # A rate configured as 0.01 (however it arrives at the boundary) must be
    # exactly Decimal("0.01"), asserted via its string form — not a float-
    # derived near-miss like Decimal("0.01000000000000000020816...").
    from_number = money(0.01)
    from_text = money("0.01")
    assert from_number == from_text == Decimal("0.01")
    assert from_number.as_tuple() == from_text.as_tuple()


# ── order-independence: the core "Done when" proof ───────────────────────────


def _cost(prompt: int, rate: str) -> Decimal:
    usage = BillableUsage(prompt_tokens=prompt)
    return compute_cost(usage, RateCard(input_cost_per_token=money(rate)))


def test_repeated_aggregation_is_order_independent_byte_identical() -> None:
    # A multiset of costs whose float summation is order-dependent
    # (0.1 + 0.2 + 0.3 != 0.3 + 0.2 + 0.1 can hold in binary float). With
    # Decimal the total must be BYTE-IDENTICAL regardless of order.
    costs = [
        _cost(1, "0.1"),
        _cost(1, "0.2"),
        _cost(1, "0.3"),
        _cost(3, "0.7"),
        _cost(7, "0.13"),
        _cost(11, "0.0000025"),
        _cost(1_000_000, "0.000001"),
    ]
    forward = sum(costs, ZERO_MONEY)
    backward = sum(reversed(costs), ZERO_MONEY)

    rng = random.Random(1337)
    shuffled = costs[:]
    rng.shuffle(shuffled)
    scrambled = sum(shuffled, ZERO_MONEY)

    assert forward == backward == scrambled
    # Byte-identical: same digits, same exponent — not merely numerically equal.
    assert str(forward) == str(backward) == str(scrambled)
    assert forward.as_tuple() == backward.as_tuple() == scrambled.as_tuple()


def test_order_independence_holds_for_a_pathological_float_case() -> None:
    # sum([0.1] * 10) == 0.9999999999999999 in float, != 1.0. Exact in Decimal.
    tenths = [money("0.1")] * 10
    assert sum(tenths, ZERO_MONEY) == Decimal("1.0")


# ── budget gate: comparison is pure Decimal, exact at the limit boundary ─────

_TEAM = uuid4()


class _FakeBudgets:
    def __init__(self, budget: Budget) -> None:
        self._budget = budget

    async def get(self, team_id: UUID) -> Budget:
        return self._budget


class _FakeUsage:
    def __init__(self, spent: Decimal) -> None:
        self._spent = spent

    async def spend_since(self, team_id: UUID, since: datetime) -> Decimal:
        return self._spent


def _meter(spent: Decimal, limit: Decimal) -> UsageMeter:
    budget = Budget(
        id=uuid4(),
        team_id=_TEAM,
        limit_cost=limit,
        window=BudgetWindow.MONTHLY,
        created_at=datetime.now(UTC),
    )
    # Minimal fakes: admit only reads spend_since / budgets.get, so cast the
    # partial fakes to their ports rather than stub every protocol method.
    return UsageMeter(
        usage=cast(UsageRepository, _FakeUsage(spent)),
        emit_trace=lambda _record: None,
        budgets=cast(BudgetRepository, _FakeBudgets(budget)),
        in_flight=InFlightSpend(),
    )


def _model() -> Model:
    return Model(
        id=uuid4(),
        team_id=_TEAM,
        name="m",
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gpt-4o",
        params={},
        api_version=None,
        input_cost_per_token=money("0.01"),
        output_cost_per_token=money("0.01"),
        enabled=True,
        created_at=datetime.now(UTC),
    )


async def test_spend_exactly_equal_to_limit_is_blocked() -> None:
    # spent == limit as EXACT Decimals: `spent + 0 >= limit` is True, so the
    # gate blocks. No float drift can push an exactly-at-cap spend under the bar.
    limit = money("0.1") + money("0.2")  # exactly Decimal("0.3"), unlike float
    meter = _meter(spent=limit, limit=limit)
    with pytest.raises(BudgetExceeded):
        await meter.admit(_TEAM, _model(), {"messages": [{"role": "user", "content": "hi"}]})


async def test_spend_one_quantum_below_limit_is_admitted() -> None:
    # One money-scale quantum (1e-12) below the cap is admitted, and the returned
    # reservation is an exact Decimal — the comparison never mixes float in.
    limit = money("0.3")
    spent = limit - Decimal(1).scaleb(-MONEY_SCALE)  # 0.299999999999
    meter = _meter(spent=spent, limit=limit)
    reservation = await meter.admit(
        _TEAM, _model(), {"messages": [{"role": "user", "content": "hi"}]}
    )
    assert isinstance(reservation, Decimal)
