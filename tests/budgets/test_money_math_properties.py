"""Property-based tests for the cost math (originally L31).

The costs these exercise are now `Decimal` (Plan 17 Slice 5), so the drift the
module was written to pin down is gone from the pricing seam itself; what remains
worth generating against is the accumulation logic — a change there must not
reintroduce order dependence or a negative total without failing here first.

The reservation-store properties below are NOT hypothesis-driven; the comment
above them explains why.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from litestar_gateway.application.usage_meter import (
    _parse_usage,
    _request_text,
    _reservation_cost,
)
from litestar_gateway.domain.entities import Model, ModelType, Provider
from litestar_gateway.domain.ports import Reservation
from litestar_gateway.domain.ports.budget_reservation import team_scope
from litestar_gateway.infrastructure.budget_reservation import (
    InMemoryBudgetReservationStore,
)

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


# ── Reservations: the store's arithmetic under any arrival order ─────────────
#
# Not hypothesis-driven, deliberately. Generating against an async store means
# `asyncio.run` inside a sync test body, which opens and closes an extra event
# loop inside a pytest-asyncio session — and that is a hazard for whatever runs
# afterwards in the same process, not a local matter (it took out an unrelated
# startup-timing test on CI while passing locally every time). Explicit
# multisets, including a reversed and a duplicate-heavy one, cover the same
# properties without nesting loops.

_MULTISETS = [
    pytest.param([1.0], id="single"),
    pytest.param([0.0, 0.0, 0.0], id="all-zero"),
    pytest.param([0.1, 0.2, 0.3], id="tenths-that-float-cannot-represent"),
    pytest.param([1e-6, 1e-6, 1e-6, 999.999999], id="tiny-and-large"),
    pytest.param([2.5] * 20, id="many-duplicates"),
    pytest.param([0.07, 12.5, 0.0, 3.33, 1e-6], id="mixed"),
]


async def _reserve_all(store, amounts: list[float]) -> list[Reservation]:
    held = []
    for amount in amounts:
        outcome = await store.try_reserve(TEAM_ID, amount, spent=0.0, limit=float("inf"), ttl_s=300)
        assert outcome.reservation is not None
        held.append(outcome.reservation)
    return held


async def _reserved_total(store) -> float:
    """Read the in-flight total by probing with a zero-amount reservation."""
    probe = await store.try_reserve(TEAM_ID, 0.0, spent=0.0, limit=float("inf"), ttl_s=300)
    if probe.reservation is not None:
        await store.release(probe.reservation)
    return probe.reserved


@pytest.mark.parametrize("amounts", _MULTISETS)
async def test_reserving_then_releasing_everything_returns_to_zero(amounts: list[float]) -> None:
    # Release is by identity, so this is exact rather than within float noise:
    # nothing is subtracted, the set simply empties.
    store = InMemoryBudgetReservationStore()
    for reservation in await _reserve_all(store, amounts):
        await store.release(reservation)

    assert await _reserved_total(store) == 0.0


@pytest.mark.parametrize("amounts", _MULTISETS)
async def test_the_reserved_total_is_order_independent(amounts: list[float]) -> None:
    # Concurrent requests are admitted in any order; the running total must not
    # depend on which one arrived first.
    forward, backward = InMemoryBudgetReservationStore(), InMemoryBudgetReservationStore()
    await _reserve_all(forward, amounts)
    await _reserve_all(backward, list(reversed(amounts)))

    assert abs(await _reserved_total(forward) - await _reserved_total(backward)) < 1e-9


@pytest.mark.parametrize("amounts", _MULTISETS)
async def test_releasing_an_unknown_reservation_changes_nothing(amounts: list[float]) -> None:
    """The property that replaced "never goes negative": you cannot release an
    amount, only a claim. A stray release deletes an id that is not there, so it
    cannot make a team's in-flight total look smaller than it is."""
    store = InMemoryBudgetReservationStore()
    await _reserve_all(store, amounts)
    before = await _reserved_total(store)

    await store.release(
        Reservation(id=uuid4(), scope=team_scope(TEAM_ID), amount=sum(amounts) * 2 + 1)
    )

    assert await _reserved_total(store) == before


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


def test_request_text_includes_chat_and_responses_structured_output_schemas() -> None:
    chat_marker = "chat-schema-marker"
    responses_marker = "responses-schema-marker"
    request = {
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "chat_answer",
                "schema": {"description": chat_marker},
            },
        },
        "text": {
            "format": {
                "type": "json_schema",
                "name": "responses_answer",
                "schema": {"description": responses_marker},
            }
        },
    }

    estimated = _request_text(request)

    assert chat_marker in estimated
    assert responses_marker in estimated
