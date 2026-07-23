"""Pre-call budget enforcement: over-budget teams are blocked before dispatch.

Unit tests for the CompletionService budget gate with fake ports. The gate
compares the team's accumulated spend in the current window against its
Budget limit and raises BudgetExceeded (402) without calling the provider.
"""

from __future__ import annotations

import gc
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.budget import window_start
from litestar_gateway.domain.callable_alias import CallableKind
from litestar_gateway.domain.entities import (
    Budget,
    BudgetWindow,
    Model,
    ModelType,
    Provider,
    TraceRecord,
    UsageEvent,
)
from litestar_gateway.domain.exceptions import BudgetExceeded, UnsupportedOperation
from litestar_gateway.domain.routing import CandidateModel, QualityTier, RouterConfig

TEAM_ID = uuid4()
KEY_ID = uuid4()


def _model(
    provider: Provider = Provider.OPENAI,
    *,
    params: dict[str, Any] | None = None,
    params_enforced: dict[str, Any] | None = None,
) -> Model:
    return Model(
        id=uuid4(),
        team_id=TEAM_ID,
        name="m",
        provider=provider,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gpt-4o",
        params=params or {},
        params_enforced=params_enforced or {},
        api_version=None,
        input_cost_per_token=0.01,
        output_cost_per_token=0.01,
        enabled=True,
        created_at=datetime.now(UTC),
    )


def _budget(limit: float, window: BudgetWindow = BudgetWindow.MONTHLY) -> Budget:
    return Budget(
        id=uuid4(),
        team_id=TEAM_ID,
        limit_cost=limit,
        window=window,
        created_at=datetime.now(UTC),
    )


class FakeModels:
    def __init__(self, model: Model) -> None:
        self._model = model

    async def get_by_name(self, team_id: UUID, name: str) -> Model | None:
        return self._model if name == self._model.name else None


class FakeCredentials:
    async def get_values(self, credential_id: UUID) -> dict[str, str] | None:
        return {"api_key": "sk-x"}  # pragma: allowlist secret


class FakeUsage:
    """Usage port with a configurable accumulated spend for the gate to read."""

    def __init__(self, spent: float = 0.0) -> None:
        self.events: list[UsageEvent] = []
        self.spent = spent
        self.spend_since_calls: list[tuple[UUID, datetime]] = []

    async def record(self, event: UsageEvent) -> None:
        self.events.append(event)

    async def enqueue_pending(self, event: UsageEvent) -> None:  # pragma: no cover
        raise AssertionError("outbox must not be used in these tests")

    async def spend_since(self, team_id: UUID, since: datetime) -> float:
        self.spend_since_calls.append((team_id, since))
        return self.spent


class FakeBudgets:
    def __init__(self, budget: Budget | None) -> None:
        self._budget = budget

    async def get(self, team_id: UUID) -> Budget | None:
        return self._budget if self._budget and self._budget.team_id == team_id else None


class CountingGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def achat_completion(self, request, model, credentials) -> dict[str, Any]:
        self.calls += 1
        return {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    async def aresponses(self, request, model, credentials) -> dict[str, Any]:
        self.calls += 1
        return {"usage": {"input_tokens": 1, "output_tokens": 1}}

    async def astream_chat_completion(self, request, model, credentials):
        self.calls += 1

        async def _stream():
            yield {"choices": [{"index": 0, "delta": {"content": "hi"}}]}

        return _stream()


class FakeCallableResolver:
    def __init__(self, router: RouterConfig, model: Model) -> None:
        self._router = router
        self._model = model

    async def resolve(self, team_id: UUID, alias: str):
        if alias == self._router.name:
            return SimpleNamespace(kind=CallableKind.ROUTER, resource=self._router)
        return None

    async def resolve_model_id(self, team_id: UUID, model_id: UUID) -> Model | None:
        return self._model if model_id == self._model.id else None


class NoSideEffectRouter:
    def __init__(self) -> None:
        self.route_calls = 0

    async def route(self, *args, **kwargs):
        self.route_calls += 1
        raise AssertionError("router strategy must not run for a rejected Responses request")


def _service(
    gateway: CountingGateway,
    usage: FakeUsage,
    budgets: FakeBudgets | None,
    model: Model | None = None,
) -> CompletionService:
    traces: list[TraceRecord] = []
    return CompletionService(
        models=FakeModels(model or _model()),  # type: ignore[arg-type]
        credentials=FakeCredentials(),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        meter=UsageMeter(
            usage=usage,  # type: ignore[arg-type]
            emit_trace=traces.append,
            budgets=budgets,  # type: ignore[arg-type]
        ),
    )


REQUEST = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


async def test_under_budget_allows_the_call() -> None:
    gateway = CountingGateway()
    service = _service(gateway, FakeUsage(spent=0.5), FakeBudgets(_budget(1.0)))

    await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert gateway.calls == 1


async def test_over_budget_blocks_before_dispatch() -> None:
    gateway = CountingGateway()
    usage = FakeUsage(spent=1.5)
    service = _service(gateway, usage, FakeBudgets(_budget(1.0)))

    with pytest.raises(BudgetExceeded):
        await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert gateway.calls == 0  # never reached the provider
    assert usage.events == []  # nothing billed for a blocked call


async def test_unsupported_responses_fail_before_budget_admission() -> None:
    gateway = CountingGateway()
    usage = FakeUsage(spent=0.0)
    service = _service(
        gateway,
        usage,
        FakeBudgets(_budget(1.0)),
        model=_model(Provider.DATABRICKS),
    )

    with pytest.raises(UnsupportedOperation, match="tools"):
        await service.responses(
            TEAM_ID,
            KEY_ID,
            {
                "model": "m",
                "input": "hi",
                "tools": [{"type": "web_search"}],
            },
        )

    assert usage.spend_since_calls == []
    assert gateway.calls == 0


async def test_unsupported_model_config_fails_before_budget_admission() -> None:
    gateway = CountingGateway()
    usage = FakeUsage(spent=0.0)
    service = _service(
        gateway,
        usage,
        FakeBudgets(_budget(1.0)),
        model=_model(
            Provider.DATABRICKS,
            params_enforced={
                "tools": [{"type": "function", "function": {"name": "weather", "parameters": {}}}]
            },
        ),
    )

    with pytest.raises(UnsupportedOperation, match=r"configured model field\(s\): tools"):
        await service.responses(TEAM_ID, KEY_ID, {"model": "m", "input": "hi"})

    assert usage.spend_since_calls == []
    assert gateway.calls == 0


async def test_native_background_fails_before_budget_admission() -> None:
    gateway = CountingGateway()
    usage = FakeUsage(spent=0.0)
    service = _service(gateway, usage, FakeBudgets(_budget(1.0)))

    with pytest.raises(UnsupportedOperation, match="background"):
        await service.responses(
            TEAM_ID,
            KEY_ID,
            {"model": "m", "input": "hi", "background": True},
        )

    assert usage.spend_since_calls == []
    assert gateway.calls == 0


async def test_router_is_prefiltered_before_strategy_side_effects() -> None:
    gateway = CountingGateway()
    usage = FakeUsage(spent=0.0)
    model = _model(Provider.DATABRICKS)
    router = RouterConfig(
        id=uuid4(),
        team_id=TEAM_ID,
        name="auto",
        candidates=(
            CandidateModel(
                model_name=model.name,
                model_id=model.id,
                description="chat-only candidate",
                quality_tier=QualityTier.MEDIUM,
                supports_tools=True,
            ),
        ),
        default_model=model.name,
        default_model_id=model.id,
        strategy="judge",
        strategy_config={},
        enabled=True,
        created_at=datetime.now(UTC),
    )
    router_service = NoSideEffectRouter()
    service = CompletionService(
        models=FakeModels(model),  # type: ignore[arg-type]
        credentials=FakeCredentials(),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        meter=UsageMeter(
            usage=usage,  # type: ignore[arg-type]
            emit_trace=lambda trace: None,
            budgets=FakeBudgets(_budget(1.0)),  # type: ignore[arg-type]
        ),
        router_service=router_service,  # type: ignore[arg-type]
        callable_resolver=FakeCallableResolver(router, model),  # type: ignore[arg-type]
    )

    with pytest.raises(UnsupportedOperation, match="tools"):
        await service.responses(
            TEAM_ID,
            KEY_ID,
            {
                "model": "auto",
                "input": "hi",
                "tools": [{"type": "web_search"}],
            },
        )

    assert router_service.route_calls == 0
    assert usage.spend_since_calls == []
    assert gateway.calls == 0


async def test_spend_exactly_at_limit_blocks() -> None:
    gateway = CountingGateway()
    service = _service(gateway, FakeUsage(spent=1.0), FakeBudgets(_budget(1.0)))

    with pytest.raises(BudgetExceeded):
        await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert gateway.calls == 0


async def test_unstarted_stream_releases_reservation() -> None:
    # M27: a stream admitted (reservation taken at the gate) but never iterated
    # — e.g. the SSE layer returns before the first byte, or the client drops —
    # must not leak its reservation into InFlightSpend. The metered generator's
    # finally only runs once started, so a finalizer covers the never-started case.
    gateway = CountingGateway()
    service = _service(gateway, FakeUsage(spent=0.0), FakeBudgets(_budget(100.0)))

    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, dict(REQUEST))
    assert service._meter._in_flight.total(TEAM_ID) > 0  # reserved at admission

    del stream  # never iterated
    gc.collect()  # collect the abandoned generator -> finalizer releases
    assert service._meter._in_flight.total(TEAM_ID) == 0  # released, not leaked


async def test_no_budget_configured_allows_the_call() -> None:
    gateway = CountingGateway()
    usage = FakeUsage(spent=1_000_000.0)
    service = _service(gateway, usage, FakeBudgets(None))

    await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert gateway.calls == 1
    assert usage.spend_since_calls == []  # no budget → no spend query


async def test_without_budget_port_service_still_works() -> None:
    # Library use: CompletionService built without a budgets repository.
    gateway = CountingGateway()
    service = _service(gateway, FakeUsage(), None)

    await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert gateway.calls == 1


async def test_gate_queries_spend_from_current_window_start() -> None:
    gateway = CountingGateway()
    usage = FakeUsage(spent=0.0)
    service = _service(gateway, usage, FakeBudgets(_budget(1.0, BudgetWindow.DAILY)))

    await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert len(usage.spend_since_calls) == 1
    team_id, since = usage.spend_since_calls[0]
    assert team_id == TEAM_ID
    assert since == window_start(BudgetWindow.DAILY, datetime.now(UTC))


async def test_streaming_is_gated_too() -> None:
    gateway = CountingGateway()
    service = _service(gateway, FakeUsage(spent=2.0), FakeBudgets(_budget(1.0)))

    with pytest.raises(BudgetExceeded):
        await service.open_chat_stream(TEAM_ID, KEY_ID, {**REQUEST, "stream": True})

    assert gateway.calls == 0


# max_tokens=100 at 0.01 USD/token reserves 1.0 USD of pessimistic output cost.
MAX_TOKENS_REQUEST = {**REQUEST, "max_tokens": 100}


async def test_inflight_reservation_blocks_a_concurrent_stream_burst() -> None:
    # Budget 1.0, spent 0.5: the first stream is admitted and reserves its
    # requested output ceiling. A second stream opened while the first is
    # still in flight must be rejected — the old gate read only committed
    # spend, so any number of streams could be admitted in that blind spot.
    gateway = CountingGateway()
    service = _service(gateway, FakeUsage(spent=0.5), FakeBudgets(_budget(1.0)))

    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, {**MAX_TOKENS_REQUEST, "stream": True})
    with pytest.raises(BudgetExceeded):
        await service.open_chat_stream(TEAM_ID, KEY_ID, {**MAX_TOKENS_REQUEST, "stream": True})
    assert gateway.calls == 1

    async for _ in stream:  # settle the admitted stream
        pass


async def test_reservation_is_released_at_stream_settlement() -> None:
    # Once a stream settles, its reservation is gone: the next request is
    # admitted again (committed spend alone still fits the budget).
    gateway = CountingGateway()
    service = _service(gateway, FakeUsage(spent=0.5), FakeBudgets(_budget(1.0)))

    first = await service.open_chat_stream(TEAM_ID, KEY_ID, {**MAX_TOKENS_REQUEST, "stream": True})
    async for _ in first:
        pass

    second = await service.open_chat_stream(TEAM_ID, KEY_ID, {**MAX_TOKENS_REQUEST, "stream": True})
    async for _ in second:
        pass
    assert gateway.calls == 2


async def test_reservation_is_released_after_a_non_stream_call() -> None:
    gateway = CountingGateway()
    service = _service(gateway, FakeUsage(spent=0.5), FakeBudgets(_budget(1.0)))

    await service.chat_completion(TEAM_ID, KEY_ID, dict(MAX_TOKENS_REQUEST))
    await service.chat_completion(TEAM_ID, KEY_ID, dict(MAX_TOKENS_REQUEST))
    assert gateway.calls == 2


async def test_reservation_is_released_when_opening_the_stream_fails() -> None:
    # A failed provider connect must not leave its reservation behind, or the
    # team would be locked out until the process restarts.
    class FailingGateway(CountingGateway):
        async def astream_chat_completion(self, request, model, credentials):
            raise RuntimeError("connect failed")

    gateway = FailingGateway()
    service = _service(gateway, FakeUsage(spent=0.5), FakeBudgets(_budget(1.0)))

    with pytest.raises(RuntimeError):
        await service.open_chat_stream(TEAM_ID, KEY_ID, {**MAX_TOKENS_REQUEST, "stream": True})

    await service.chat_completion(TEAM_ID, KEY_ID, dict(MAX_TOKENS_REQUEST))
    assert gateway.calls == 1


async def test_multi_choice_stream_reserves_output_per_choice() -> None:
    # n=8 regenerates the output ceiling per choice: the reservation must be
    # ~8x the single-choice one, or a near-exhausted budget admits a burst
    # whose real committed cost is 8x what the gate accounted for.
    gateway = CountingGateway()
    service = _service(gateway, FakeUsage(spent=0.0), FakeBudgets(_budget(1.0)))

    # max_tokens=20 at 0.01 → 0.2 per choice; x8 choices reserves 1.6 ≥ 1.0.
    stream = await service.open_chat_stream(
        TEAM_ID, KEY_ID, {**REQUEST, "max_tokens": 20, "n": 8, "stream": True}
    )
    with pytest.raises(BudgetExceeded):
        await service.chat_completion(TEAM_ID, KEY_ID, {**REQUEST, "max_tokens": 20})
    assert gateway.calls == 1

    async for _ in stream:  # settle: the n-scaled reservation is fully released
        pass
    await service.chat_completion(TEAM_ID, KEY_ID, {**REQUEST, "max_tokens": 20})
    assert gateway.calls == 2


async def test_single_choice_stream_does_not_over_reserve() -> None:
    # Same shape with n=1: 0.2 reserved, the concurrent call must still pass.
    gateway = CountingGateway()
    service = _service(gateway, FakeUsage(spent=0.0), FakeBudgets(_budget(1.0)))

    stream = await service.open_chat_stream(
        TEAM_ID, KEY_ID, {**REQUEST, "max_tokens": 20, "n": 1, "stream": True}
    )
    await service.chat_completion(TEAM_ID, KEY_ID, {**REQUEST, "max_tokens": 20})
    assert gateway.calls == 2

    async for _ in stream:
        pass


async def test_reservation_uses_the_clamped_max_tokens() -> None:
    # An absurd client max_tokens must not poison the gate: the dispatched
    # request is clamped to MAX_TOKENS (32k → 320.0 reserved at 0.01), so a
    # normal concurrent call under a 1000.0 budget is still admitted. Before
    # the fix the reservation was taken from the raw value (~10M USD) and
    # locked the whole team out until the stream settled.
    gateway = CountingGateway()
    service = _service(gateway, FakeUsage(spent=0.0), FakeBudgets(_budget(1000.0)))

    stream = await service.open_chat_stream(
        TEAM_ID, KEY_ID, {**REQUEST, "max_tokens": 999_999_999, "stream": True}
    )
    await service.chat_completion(TEAM_ID, KEY_ID, {**REQUEST, "max_tokens": 100})
    assert gateway.calls == 2

    async for _ in stream:
        pass


@pytest.mark.parametrize("provider", [Provider.ANTHROPIC, Provider.VERTEX_AI, Provider.BEDROCK])
async def test_n_gt_1_rejected_for_provider_that_ignores_n(provider: Provider) -> None:
    # R7-M50: Anthropic/Vertex/Bedrock chat translators never forward `n` and
    # always return exactly one completion, yet the reservation multiplied the
    # output ceiling by n (up to MAX_N=8), spuriously tripping BudgetExceeded.
    # A chat request with n>1 on those providers is rejected before dispatch —
    # never reaching the provider and never taking an inflated reservation.
    gateway = CountingGateway()
    service = _service(
        gateway, FakeUsage(spent=0.0), FakeBudgets(_budget(1.0)), model=_model(provider)
    )

    with pytest.raises(UnsupportedOperation):
        await service.chat_completion(TEAM_ID, KEY_ID, {**MAX_TOKENS_REQUEST, "n": 4})

    assert gateway.calls == 0  # never dispatched
    assert service._meter._in_flight.total(TEAM_ID) == 0  # no over-reservation held


async def test_n_1_allowed_for_provider_that_ignores_n() -> None:
    # n=1 (or absent) is fine everywhere — only n>1 is rejected on those providers.
    gateway = CountingGateway()
    service = _service(
        gateway, FakeUsage(spent=0.0), FakeBudgets(_budget(1.0)), model=_model(Provider.ANTHROPIC)
    )

    await service.chat_completion(TEAM_ID, KEY_ID, {**MAX_TOKENS_REQUEST, "n": 1})
    await service.chat_completion(TEAM_ID, KEY_ID, dict(MAX_TOKENS_REQUEST))  # n absent
    assert gateway.calls == 2


async def test_n_gt_1_still_allowed_for_openai() -> None:
    # OpenAI/Azure/Databricks forward `n`, so multi-completion requests keep
    # working and legitimately reserve output per choice.
    gateway = CountingGateway()
    service = _service(
        gateway, FakeUsage(spent=0.0), FakeBudgets(_budget(100.0)), model=_model(Provider.OPENAI)
    )

    await service.chat_completion(TEAM_ID, KEY_ID, {**MAX_TOKENS_REQUEST, "n": 4})
    assert gateway.calls == 1
