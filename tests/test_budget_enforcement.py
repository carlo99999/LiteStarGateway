"""Pre-call budget enforcement: over-budget teams are blocked before dispatch.

Unit tests for the CompletionService budget gate with fake ports. The gate
compares the team's accumulated spend in the current window against its
Budget limit and raises BudgetExceeded (402) without calling the provider.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.budget import window_start
from litestar_gateway.domain.entities import (
    Budget,
    BudgetWindow,
    Model,
    ModelType,
    Provider,
    TraceRecord,
    UsageEvent,
)
from litestar_gateway.domain.exceptions import BudgetExceeded

TEAM_ID = uuid4()
KEY_ID = uuid4()


def _model() -> Model:
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

    async def astream_chat_completion(self, request, model, credentials):
        self.calls += 1

        async def _stream():
            yield {"choices": [{"index": 0, "delta": {"content": "hi"}}]}

        return _stream()


def _service(
    gateway: CountingGateway,
    usage: FakeUsage,
    budgets: FakeBudgets | None,
) -> CompletionService:
    traces: list[TraceRecord] = []
    return CompletionService(
        models=FakeModels(_model()),  # type: ignore[arg-type]
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


async def test_spend_exactly_at_limit_blocks() -> None:
    gateway = CountingGateway()
    service = _service(gateway, FakeUsage(spent=1.0), FakeBudgets(_budget(1.0)))

    with pytest.raises(BudgetExceeded):
        await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert gateway.calls == 0


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
