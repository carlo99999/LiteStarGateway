"""Settlement and reservation for the non-token billing dimensions (Plan 13
Phase 1): image generation and Anthropic prompt-cache tokens both price through
the one normalized pricing function, so a reservation can never under-charge
relative to the eventual settlement, and the ledger records each dimension.

Exercised directly against `UsageMeter` (no HTTP app) with a fake repository
that captures the recorded `UsageEvent`s — the money the ledger actually sees.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from litestar_gateway.application.usage_meter import UsageMeter, _reservation_cost
from litestar_gateway.domain.entities import (
    ApiKeySpend,
    Model,
    Provider,
    UsageAggregate,
    UsageBucket,
    UsageEvent,
)
from litestar_gateway.domain.entities.enums import ModelType

TEAM_ID = uuid4()


class _FakeUsageRepository:
    def __init__(self) -> None:
        self.recorded: list[UsageEvent] = []

    async def record(self, event: UsageEvent) -> None:
        self.recorded.append(event)

    async def list_events(self, team_id: UUID, **_: Any) -> list[UsageEvent]:
        return []

    async def aggregate(self, team_id: UUID, **_: Any) -> list[UsageAggregate]:
        return []

    async def spend_by_api_key(self, team_id: UUID) -> list[ApiKeySpend]:
        return []

    async def spend_since(self, team_id: UUID, since: datetime) -> float:
        return 0.0

    async def enqueue_pending(self, event: UsageEvent) -> None:
        raise AssertionError("outbox must not be used in this test")

    async def reconcile_pending(self, *, limit: int = 100) -> int:
        return 0

    async def cache_savings(self, team_id: UUID) -> tuple[float, int, int, int]:
        return (0.0, 0, 0, 0)

    async def platform_cache_savings(self) -> tuple[float, int, int, int]:
        return (0.0, 0, 0, 0)

    async def timeseries(self, team_id: UUID, **_: Any) -> list[UsageBucket]:
        return []


def _model(
    *,
    model_type: ModelType = ModelType.CHAT,
    provider: Provider = Provider.OPENAI,
    input_cost_per_token: float | None = None,
    output_cost_per_token: float | None = None,
    cache_write_cost_per_token: float | None = None,
    cache_read_cost_per_token: float | None = None,
    image_cost_per_image: float | None = None,
    image_prices: dict[str, float] | None = None,
) -> Model:
    return Model(
        id=uuid4(),
        team_id=TEAM_ID,
        name="m",
        provider=provider,
        credential_id=uuid4(),
        type=model_type,
        provider_model_id="m",
        params={},
        api_version=None,
        input_cost_per_token=input_cost_per_token,
        output_cost_per_token=output_cost_per_token,
        enabled=True,
        created_at=datetime.now(UTC),
        cache_write_cost_per_token=cache_write_cost_per_token,
        cache_read_cost_per_token=cache_read_cost_per_token,
        image_cost_per_image=image_cost_per_image,
        image_prices=image_prices or {},
    )


def _meter(repo: _FakeUsageRepository) -> UsageMeter:
    return UsageMeter(repo, lambda _t: None)


# ── Image settlement ──────────────────────────────────────────────────────────


async def test_image_generation_bills_per_image_and_records_the_count() -> None:
    repo = _FakeUsageRepository()
    model = _model(model_type=ModelType.IMAGE, provider=Provider.OPENAI, image_cost_per_image=0.04)

    _, _, cost = await (_meter(repo)).settle_ok(
        team_id=TEAM_ID,
        api_key_id=None,
        model=model,
        operation="images",
        response={"data": [{"url": "a"}, {"url": "b"}, {"url": "c"}]},
        latency_ms=5.0,
        request={"n": 3, "size": "1024x1024", "quality": "standard"},
    )

    assert cost == 0.12  # 3 images * 0.04
    event = repo.recorded[0]
    assert event.image_count == 3
    assert event.cost == 0.12
    assert (event.prompt_tokens, event.completion_tokens) == (0, 0)


async def test_image_size_quality_specific_price_beats_flat_fallback() -> None:
    repo = _FakeUsageRepository()
    model = _model(
        model_type=ModelType.IMAGE,
        image_cost_per_image=0.04,
        image_prices={"1024x1024/hd": 0.08},
    )
    _, _, cost = await (_meter(repo)).settle_ok(
        team_id=TEAM_ID,
        api_key_id=None,
        model=model,
        operation="images",
        response={"data": [{"url": "a"}, {"url": "b"}]},
        latency_ms=5.0,
        request={"n": 2, "size": "1024x1024", "quality": "hd"},
    )
    assert cost == 0.16  # 2 * 0.08


async def test_image_reservation_is_an_upper_bound_on_settlement() -> None:
    # design: reservation uses the requested `n`; settlement bills the images
    # actually returned — so a request that returns fewer images never exceeds
    # its reservation.
    model = _model(model_type=ModelType.IMAGE, image_cost_per_image=0.04)
    reservation = _reservation_cost(model, {"n": 3, "size": "1024x1024", "quality": "standard"})
    assert reservation == 0.12

    repo = _FakeUsageRepository()
    _, _, settled = await (_meter(repo)).settle_ok(
        team_id=TEAM_ID,
        api_key_id=None,
        model=model,
        operation="images",
        response={"data": [{"url": "a"}]},  # provider returned only 1
        latency_ms=5.0,
        request={"n": 3, "size": "1024x1024", "quality": "standard"},
    )
    assert settled == 0.04
    assert settled <= reservation


# ── Anthropic cache-token settlement ──────────────────────────────────────────


async def test_cache_tokens_settle_at_their_own_rates_and_are_recorded() -> None:
    repo = _FakeUsageRepository()
    model = _model(
        provider=Provider.ANTHROPIC,
        input_cost_per_token=0.001,
        output_cost_per_token=0.002,
        cache_write_cost_per_token=0.00125,
        cache_read_cost_per_token=0.0001,
    )
    _, _, cost = await (_meter(repo)).settle_ok(
        team_id=TEAM_ID,
        api_key_id=None,
        model=model,
        operation="chat.completions",
        response={
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "cache_creation_input_tokens": 200,
                "cache_read_input_tokens": 400,
            }
        },
        latency_ms=5.0,
    )
    expected = 100 * 0.001 + 50 * 0.002 + 200 * 0.00125 + 400 * 0.0001
    assert cost == expected
    event = repo.recorded[0]
    assert event.cache_write_tokens == 200
    assert event.cache_read_tokens == 400
    assert event.prompt_tokens == 100
    assert event.completion_tokens == 50


async def test_cache_reservation_prices_prompt_at_the_priciest_input_side_rate() -> None:
    # When cache-write costs more than ordinary input, the reservation must price
    # the prompt estimate at that higher rate so settlement (which may bill the
    # whole prompt as cache-write) can never exceed the reservation.
    request = {"messages": [{"role": "user", "content": "x" * 400}], "max_tokens": 100}
    cheap = _model(input_cost_per_token=0.001, output_cost_per_token=0.002)
    with_cache = _model(
        input_cost_per_token=0.001,
        output_cost_per_token=0.002,
        cache_write_cost_per_token=0.00125,
    )
    assert _reservation_cost(with_cache, request) > _reservation_cost(cheap, request)


# ── Strict regression: plain token calls are unchanged ────────────────────────


async def test_plain_chat_call_bills_and_reserves_exactly_as_before() -> None:
    repo = _FakeUsageRepository()
    model = _model(input_cost_per_token=0.001, output_cost_per_token=0.002)

    _, _, cost = await (_meter(repo)).settle_ok(
        team_id=TEAM_ID,
        api_key_id=None,
        model=model,
        operation="chat.completions",
        response={"usage": {"prompt_tokens": 1000, "completion_tokens": 500}},
        latency_ms=5.0,
    )
    assert cost == 1000 * 0.001 + 500 * 0.002  # 2.0
    event = repo.recorded[0]
    assert (event.cache_write_tokens, event.cache_read_tokens, event.image_count) == (0, 0, 0)

    # Reservation reduces to prompt_estimate*input + max_tokens*n*output, exactly
    # the pre-Plan-13 formula (no cache/image rates configured).
    request = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 100}
    prompt_chars = len("hello")
    prompt_est = (prompt_chars + 3) // 4
    expected_reservation = prompt_est * 0.001 + 100 * 1 * 0.002
    assert _reservation_cost(model, request) == expected_reservation
