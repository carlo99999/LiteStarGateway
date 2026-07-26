"""Cross-provider failover for streaming calls, pre-first-byte only (Plan 05
Phase 2). A failover-eligible error at stream *open* or the first `anext`
retries the next candidate; once a chunk has reached the caller, `_prime`
has already returned and no later error can enter this retry loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from litestar_gateway.application.callable_aliases import ResolvedCallable
from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.callable_alias import (
    CallableAliasBinding,
    CallableKind,
    CallableOrigin,
)
from litestar_gateway.domain.entities import Budget, BudgetWindow, Model, ModelType, Provider
from litestar_gateway.domain.exceptions import (
    UpstreamRequestRejected,
    UpstreamUnavailable,
)
from litestar_gateway.domain.routing import (
    CandidateModel,
    QualityTier,
    RouterConfig,
    RoutingDecision,
)

TEAM_ID = uuid4()
KEY_ID = uuid4()


def _model(name: str) -> Model:
    return Model(
        id=uuid4(),
        team_id=TEAM_ID,
        name=name,
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gpt-4o",
        params={},
        params_enforced={},
        api_version=None,
        input_cost_per_token=0.01,
        output_cost_per_token=0.01,
        enabled=True,
        created_at=datetime.now(UTC),
    )


def _budget(limit: float) -> Budget:
    return Budget(
        id=uuid4(),
        team_id=TEAM_ID,
        limit_cost=limit,
        window=BudgetWindow.MONTHLY,
        created_at=datetime.now(UTC),
    )


def _router(primary: Model, secondary: Model, *, max_attempts: int = 2) -> RouterConfig:
    return RouterConfig(
        id=uuid4(),
        team_id=TEAM_ID,
        name="auto",
        candidates=(
            CandidateModel(
                model_name=primary.name,
                model_id=primary.id,
                description="primary",
                quality_tier=QualityTier.MEDIUM,
            ),
            CandidateModel(
                model_name=secondary.name,
                model_id=secondary.id,
                description="secondary",
                quality_tier=QualityTier.MEDIUM,
            ),
        ),
        default_model=primary.name,
        default_model_id=primary.id,
        strategy="complexity",
        strategy_config={},
        enabled=True,
        created_at=datetime.now(UTC),
        failover_enabled=True,
        max_attempts=max_attempts,
    )


class FakeCredentials:
    async def get_values(self, credential_id: UUID) -> dict[str, str] | None:
        return {"api_key": "sk-x"}  # pragma: allowlist secret


class FakeUsage:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def record(self, event: Any) -> None:
        self.events.append(event)

    async def enqueue_pending(self, event: Any) -> None:  # pragma: no cover
        raise AssertionError("outbox must not be used in these tests")

    async def spend_since(self, team_id: UUID, since: datetime) -> float:
        return 0.0


class FakeBudgets:
    def __init__(self, budget: Budget | None) -> None:
        self._budget = budget

    async def get(self, team_id: UUID) -> Budget | None:
        return self._budget


class MultiModelCallableResolver:
    def __init__(self, router: RouterConfig, models: dict[UUID, Model]) -> None:
        self._router = router
        self._models = models

    async def resolve(self, team_id: UUID, alias: str) -> ResolvedCallable | None:
        if alias == self._router.name:
            return ResolvedCallable(
                effective_alias=alias,
                binding=CallableAliasBinding(
                    id=uuid4(),
                    team_id=team_id,
                    alias=alias,
                    kind=CallableKind.ROUTER,
                    resource_id=self._router.id,
                    origin=CallableOrigin.OWN,
                    source_team_id=team_id,
                ),
                resource=self._router,
            )
        return None

    async def resolve_model_id(self, team_id: UUID, model_id: UUID) -> Model | None:
        return self._models.get(model_id)


class FixedDecisionRouter:
    def __init__(self, chosen: Model) -> None:
        self._chosen = chosen

    async def route(self, router, request, *, acting_team_id, api_key_id) -> RoutingDecision:
        return RoutingDecision(
            model_name=self._chosen.name,
            strategy="fixed",
            tier=None,
            score=None,
            signals=(),
            decision_ms=0.0,
            model_id=self._chosen.id,
        )

    async def record_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        return None


def _ok_chunks() -> list[dict[str, Any]]:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": "hi"}}]},
    ]


class ScriptedStreamGateway:
    """Each scripted step is either an exception (raised at `astream_chat_completion`
    open time), "first_chunk_error" (the returned generator raises on its very
    first `anext`), or "after_first_chunk_error" (yields one real chunk, then
    raises). Exhausting the script yields `_ok_chunks()`."""

    def __init__(self, script: list[Exception | str]) -> None:
        self._script = list(script)
        self.calls: list[Model] = []

    async def astream_chat_completion(self, request, model, credentials):
        self.calls.append(model)
        if self._script:
            step = self._script.pop(0)
            if isinstance(step, Exception):
                raise step
            if step == "first_chunk_error":
                return self._raise_on_first_chunk()
            assert step == "after_first_chunk_error"
            return self._raise_after_first_chunk()
        return self._ok_stream()

    async def _raise_on_first_chunk(self) -> AsyncIterator[dict[str, Any]]:
        raise UpstreamUnavailable("failed on first chunk")
        yield {}  # pragma: no cover - unreachable; keeps this an async generator

    async def _raise_after_first_chunk(self) -> AsyncIterator[dict[str, Any]]:
        # A real content chunk, not just the role announcement, so streamed
        # output is non-zero and the M26 zero-consumption skip doesn't apply.
        yield _ok_chunks()[1]
        raise UpstreamUnavailable("failed mid-stream, after the first chunk")

    async def _ok_stream(self) -> AsyncIterator[dict[str, Any]]:
        for chunk in _ok_chunks():
            yield chunk


def _service(
    gateway: ScriptedStreamGateway,
    usage: FakeUsage,
    router: RouterConfig,
    models: dict[UUID, Model],
    router_service: FixedDecisionRouter,
) -> CompletionService:
    return CompletionService(
        models=SimpleNamespace(get_by_name=None),  # type: ignore[arg-type]
        credentials=FakeCredentials(),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        meter=UsageMeter(
            usage=usage,  # type: ignore[arg-type]
            emit_trace=lambda trace: None,
            budgets=FakeBudgets(_budget(1000.0)),  # type: ignore[arg-type]
        ),
        router_service=router_service,  # type: ignore[arg-type]
        callable_resolver=MultiModelCallableResolver(router, models),  # type: ignore[arg-type]
    )


REQUEST = {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}


async def test_error_at_stream_open_fails_over() -> None:
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = ScriptedStreamGateway([UpstreamUnavailable("503 at open")])
    usage = FakeUsage()
    service = _service(gateway, usage, router, models, FixedDecisionRouter(primary))

    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, dict(REQUEST))
    chunks = [chunk async for chunk in stream]

    assert chunks == _ok_chunks()
    assert gateway.calls == [primary, secondary]
    # Exactly one settlement: the successful stream on the serving candidate.
    # Nothing was billed for the failed-open attempt on the primary.
    assert len(usage.events) == 1
    assert usage.events[0].canonical_model_name == "secondary"


async def test_error_at_first_chunk_fails_over() -> None:
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = ScriptedStreamGateway(["first_chunk_error"])
    usage = FakeUsage()
    service = _service(gateway, usage, router, models, FixedDecisionRouter(primary))

    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, dict(REQUEST))
    chunks = [chunk async for chunk in stream]

    assert chunks == _ok_chunks()
    assert gateway.calls == [primary, secondary]
    # Exactly one settlement: the successful stream on the serving candidate.
    # The failed first-chunk attempt billed nothing (M26: zero chunks produced).
    assert len(usage.events) == 1
    assert usage.events[0].canonical_model_name == "secondary"


async def test_terminal_error_never_retries_streaming() -> None:
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = ScriptedStreamGateway([UpstreamRequestRejected("bad request")])
    usage = FakeUsage()
    service = _service(gateway, usage, router, models, FixedDecisionRouter(primary))

    with pytest.raises(UpstreamRequestRejected):
        await service.open_chat_stream(TEAM_ID, KEY_ID, dict(REQUEST))

    assert gateway.calls == [primary]
    assert usage.events == []


async def test_exhausting_max_attempts_surfaces_the_last_streaming_error() -> None:
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary, max_attempts=2)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = ScriptedStreamGateway([UpstreamUnavailable("503-a"), UpstreamUnavailable("503-b")])
    usage = FakeUsage()
    service = _service(gateway, usage, router, models, FixedDecisionRouter(primary))

    with pytest.raises(UpstreamUnavailable, match="503-b"):
        await service.open_chat_stream(TEAM_ID, KEY_ID, dict(REQUEST))

    assert gateway.calls == [primary, secondary]
    assert usage.events == []


async def test_error_after_first_chunk_never_fails_over() -> None:
    # Hard-boundary regression: once _prime has yielded a chunk to the
    # caller, this method has already returned -- a later error can only
    # abort the connection, never retry a different candidate.
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = ScriptedStreamGateway(["after_first_chunk_error"])
    usage = FakeUsage()
    service = _service(gateway, usage, router, models, FixedDecisionRouter(primary))

    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, dict(REQUEST))
    with pytest.raises(UpstreamUnavailable, match="mid-stream"):
        async for _ in stream:
            pass

    assert gateway.calls == [primary]  # never reached the second candidate
    assert len(usage.events) == 1  # billed what streamed before the failure
