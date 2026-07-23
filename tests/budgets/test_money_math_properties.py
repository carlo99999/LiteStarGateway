"""Property-based tests for the float-based cost math (L31).

Costs and budgets are plain `float` throughout (see `domain/entities.py`,
`usage_meter.py`) rather than `Decimal`/integer micro-USD. That's an accepted
tradeoff for now (see ISSUES round-6 L31) — these tests don't change the
representation, they document and pin down the float-precision behavior the
budget gate actually relies on, so a future change to the accumulation logic
can't silently introduce drift without failing a test first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from hypothesis import given
from hypothesis import strategies as st

from litestar_gateway.application.usage_meter import (
    InFlightSpend,
    _parse_usage,
    _request_text,
    _reservation_cost,
)
from litestar_gateway.domain.entities import Model, ModelType, Provider

TEAM_ID = uuid4()

# Realistic ranges: per-token USD prices are tiny; token counts are bounded
# (a request/response isn't going to carry billions of tokens).
_PRICE = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
_TOKENS = st.integers(min_value=0, max_value=2_000_000)
_AMOUNT = st.floats(min_value=0.0, max_value=1_000.0, allow_nan=False, allow_infinity=False)


def _model(input_cost: float | None, output_cost: float | None) -> Model:
    return Model(
        id=uuid4(),
        team_id=TEAM_ID,
        name="m",
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gpt-4o",
        params={},
        api_version=None,
        input_cost_per_token=input_cost,
        output_cost_per_token=output_cost,
        enabled=True,
        created_at=datetime.now(UTC),
    )


# ── InFlightSpend: reservation add/remove round-trips ────────────────────────


@given(amounts=st.lists(_AMOUNT, min_size=1, max_size=50))
def test_in_flight_spend_add_then_remove_all_returns_to_zero(amounts: list[float]) -> None:
    spend = InFlightSpend()
    for amount in amounts:
        spend.add(TEAM_ID, amount)
    for amount in amounts:
        spend.remove(TEAM_ID, amount)
    # Float summation isn't exactly associative, but removing exactly what was
    # added (same values, any order) must land within float noise of zero —
    # not accumulate a persistent drift.
    assert abs(spend.total(TEAM_ID)) < 1e-6


@given(amounts=st.lists(_AMOUNT, min_size=1, max_size=50))
def test_in_flight_spend_never_goes_negative(amounts: list[float]) -> None:
    spend = InFlightSpend()
    total_added = 0.0
    for amount in amounts:
        spend.add(TEAM_ID, amount)
        total_added += amount
    # Remove more than was ever added — must clamp at zero, never go negative
    # (a negative in-flight reservation would let a team's committed spend
    # look smaller than it is, widening the budget gate).
    spend.remove(TEAM_ID, total_added * 2 + 1.0)
    assert spend.total(TEAM_ID) == 0.0


@given(amounts=st.lists(_AMOUNT, min_size=2, max_size=20))
def test_in_flight_spend_total_is_order_independent(amounts: list[float]) -> None:
    forward = InFlightSpend()
    for amount in amounts:
        forward.add(TEAM_ID, amount)

    backward = InFlightSpend()
    for amount in reversed(amounts):
        backward.add(TEAM_ID, amount)

    # Same multiset of reservations, different arrival order (concurrent
    # requests can be admitted in any order) — the running total must agree
    # within float noise, not diverge based on ordering.
    assert abs(forward.total(TEAM_ID) - backward.total(TEAM_ID)) < 1e-9


# ── Cost accumulation: non-negativity and monotonicity ───────────────────────


@given(
    input_cost=_PRICE,
    output_cost=_PRICE,
    prompt_tokens=_TOKENS,
    completion_tokens=_TOKENS,
)
def test_parsed_usage_cost_is_never_negative(
    input_cost: float, output_cost: float, prompt_tokens: int, completion_tokens: int
) -> None:
    model = _model(input_cost, output_cost)
    _, _, cost = _parse_usage(
        model, {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    )
    assert cost >= 0.0


@given(
    input_cost=_PRICE,
    output_cost=_PRICE,
    prompt_tokens=_TOKENS,
    completion_tokens=_TOKENS,
    extra_completion_tokens=st.integers(min_value=0, max_value=1_000_000),
)
def test_parsed_usage_cost_is_monotonic_in_completion_tokens(
    input_cost: float,
    output_cost: float,
    prompt_tokens: int,
    completion_tokens: int,
    extra_completion_tokens: int,
) -> None:
    model = _model(input_cost, output_cost)
    _, _, smaller = _parse_usage(
        model, {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    )
    _, _, larger = _parse_usage(
        model,
        {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens + extra_completion_tokens,
        },
    )
    # Billing more completion tokens (same prices) must never reduce the cost.
    assert larger >= smaller


@given(input_cost=_PRICE, output_cost=_PRICE, prompt_tokens=_TOKENS, completion_tokens=_TOKENS)
def test_parsed_usage_accepts_responses_api_token_shape(
    input_cost: float, output_cost: float, prompt_tokens: int, completion_tokens: int
) -> None:
    model = _model(input_cost, output_cost)
    chat_prompt, chat_completion, chat_cost = _parse_usage(
        model, {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
    )
    responses_prompt, responses_completion, responses_cost = _parse_usage(
        model, {"input_tokens": prompt_tokens, "output_tokens": completion_tokens}
    )
    # Same token counts billed through either provider shape must cost the
    # same — the gateway bills a provider-agnostic OpenAI-shaped view.
    assert (chat_prompt, chat_completion) == (responses_prompt, responses_completion)
    assert chat_cost == responses_cost


@given(
    input_cost=_PRICE,
    output_cost=_PRICE,
    max_tokens=st.integers(min_value=1, max_value=100_000),
)
def test_reservation_cost_is_never_negative(
    input_cost: float, output_cost: float, max_tokens: int
) -> None:
    model = _model(input_cost, output_cost)
    reservation = _reservation_cost(model, {"messages": [{"role": "user", "content": "hi"}]})
    assert reservation >= 0.0
    reservation_with_ceiling = _reservation_cost(
        model, {"messages": [{"role": "user", "content": "hi"}], "max_tokens": max_tokens}
    )
    assert reservation_with_ceiling >= 0.0
    # A higher output ceiling (same prompt) reserves at least as much.
    assert reservation_with_ceiling >= reservation


def test_request_text_includes_anthropic_system_field() -> None:
    # R8-ISSUE-008: the Anthropic-native top-level `system` prompt (string or
    # content-block list) must be counted by the reservation/estimate, not only
    # `messages` — otherwise a large system prompt is invisible to admission.
    assert "big system" in _request_text({"system": "big system", "messages": []})
    assert "block sys" in _request_text(
        {"system": [{"type": "text", "text": "block sys"}], "messages": []}
    )
    # Regression guard: system text actually raises the estimated prompt cost.
    model = _model(input_cost=1.0, output_cost=1.0)
    with_system = _reservation_cost(model, {"system": "x" * 400, "messages": []})
    without_system = _reservation_cost(model, {"messages": []})
    assert with_system > without_system


def test_request_text_and_reservation_include_tool_payloads() -> None:
    model = _model(input_cost=1.0, output_cost=1.0)
    call_id = "call_" + ("i" * 400)
    function_name = "n" * 400
    request = {
        "input": [
            {
                "type": "function_call",
                "call_id": call_id,
                "name": function_name,
                "arguments": '{"query":"' + ("x" * 400) + '"}',
            },
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": "y" * 400,
            },
        ],
        "tools": [
            {
                "type": "function",
                "name": "lookup",
                "parameters": {
                    "type": "object",
                    "description": "z" * 400,
                },
            }
        ],
    }

    assert call_id in _request_text(request)
    assert function_name in _request_text(request)
    assert "x" * 400 in _request_text(request)
    assert "y" * 400 in _request_text(request)
    assert "z" * 400 in _request_text(request)
    assert _reservation_cost(model, request) > _reservation_cost(model, {"input": []})
