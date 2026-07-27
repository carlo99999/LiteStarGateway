"""Routed streams must settle usage onto the routing decision exactly like
non-streamed routed calls already do (Plan 10 Phase 0 — routed stream
settlement). Before this, `CompletionService._attach_routing_usage` only ran
for the non-streaming `_dispatch` path; a stream's settled usage never
reached `RouterService.record_usage`, leaving every routed stream out of the
router's savings tracking.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import anyio
import pytest

from litestar_gateway.application.callable_aliases import ResolvedCallable
from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.callable_alias import (
    CallableAliasBinding,
    CallableKind,
    CallableOrigin,
)
from litestar_gateway.domain.entities import Model, ModelType, Provider, UsageEvent
from litestar_gateway.domain.routing import (
    CandidateModel,
    QualityTier,
    RouterConfig,
    RoutingDecision,
)

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
        input_cost_per_token=None,
        output_cost_per_token=None,
        enabled=True,
        created_at=datetime.now(UTC),
    )


def _router(model: Model) -> RouterConfig:
    return RouterConfig(
        id=uuid4(),
        team_id=TEAM_ID,
        name="auto",
        candidates=(
            CandidateModel(
                model_name=model.name,
                model_id=model.id,
                description="only",
                quality_tier=QualityTier.MEDIUM,
            ),
        ),
        default_model=model.name,
        default_model_id=model.id,
        strategy="complexity",
        strategy_config={},
        enabled=True,
        created_at=datetime.now(UTC),
    )


class FakeCredentials:
    async def get_values(self, credential_id: UUID) -> dict[str, str] | None:
        return {"api_key": "sk-x"}  # pragma: allowlist secret


class FakeUsage:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    async def record(self, event: UsageEvent) -> None:
        # The real repository's first await is a DB roundtrip — a cancellation
        # checkpoint. Mirror it so a cancelled scope re-raises here like in prod.
        await anyio.lowlevel.checkpoint()
        self.events.append(event)

    async def enqueue_pending(self, event: UsageEvent) -> None:  # pragma: no cover
        raise AssertionError("outbox must not be used in these tests")


class MultiModelCallableResolver:
    def __init__(self, router: RouterConfig, model: Model) -> None:
        self._router = router
        self._model = model

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
        return self._model if model_id == self._model.id else None


class RecordingRouter:
    """A `RouterService` double that always routes to the fixed model and
    records every `record_usage` call, so tests can assert the settled
    (prompt, completion) pair attached to the routing decision."""

    def __init__(self, chosen: Model) -> None:
        self._chosen = chosen
        self.usage_calls: list[tuple[int, int]] = []

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

    async def record_failover(self, attempts: int, failover_used: bool) -> None:
        return None

    async def record_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.usage_calls.append((prompt_tokens, completion_tokens))


class RaisingRouter(RecordingRouter):
    """Simulates a bug in the analytics-attachment call itself (e.g. a
    `RouterService.record_usage` regression). Must never break the stream or
    the billing settlement that already happened."""

    async def record_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.usage_calls.append((prompt_tokens, completion_tokens))
        raise RuntimeError("boom: analytics attachment bug")


def _chat_chunks() -> list[dict[str, Any]]:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": "abcdefgh"}}]},  # 8 chars -> 2 tokens
        {"choices": [{"index": 0, "delta": {"content": "ijkl"}}]},  # 4 chars -> 1 token
    ]


class StreamGateway:
    def __init__(self, chunks: list[dict[str, Any]], *, fail_after: bool = False) -> None:
        self._chunks = chunks
        self._fail_after = fail_after

    async def astream_chat_completion(self, request, model, credentials):
        return self._stream()

    async def _stream(self) -> AsyncIterator[dict[str, Any]]:
        for chunk in self._chunks:
            yield chunk
        if self._fail_after:
            raise RuntimeError("provider died mid-stream")


def _service(
    gateway: Any, usage: FakeUsage, router: RouterConfig, model: Model, router_service: Any
) -> CompletionService:
    return CompletionService(
        models=SimpleNamespace(get_by_name=None),  # type: ignore[arg-type]
        credentials=FakeCredentials(),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        meter=UsageMeter(usage=usage, emit_trace=lambda trace: None),  # type: ignore[arg-type]
        router_service=router_service,
        callable_resolver=MultiModelCallableResolver(router, model),  # type: ignore[arg-type]
    )


# 4-char strings estimate to exactly 1 token each (4 chars/token heuristic).
REQUEST = {"model": "auto", "messages": [{"role": "user", "content": "hiya"}]}


async def test_normal_stream_attaches_real_settled_usage_to_routing_decision() -> None:
    model = _model()
    router = _router(model)
    usage = FakeUsage()
    router_service = RecordingRouter(model)
    chunks = [
        *_chat_chunks(),
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7}},
    ]
    service = _service(StreamGateway(chunks), usage, router, model, router_service)

    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, dict(REQUEST))
    async for _ in stream:
        pass

    assert len(usage.events) == 1
    assert usage.events[0].prompt_tokens == 5
    assert usage.events[0].completion_tokens == 7
    # The routing decision gets the exact same settled counts as the ledger.
    assert router_service.usage_calls == [(5, 7)]


async def test_mid_stream_provider_error_attaches_partial_estimated_usage() -> None:
    model = _model()
    router = _router(model)
    usage = FakeUsage()
    router_service = RecordingRouter(model)
    service = _service(
        StreamGateway(_chat_chunks(), fail_after=True), usage, router, model, router_service
    )

    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, dict(REQUEST))
    with pytest.raises(RuntimeError):
        async for _ in stream:
            pass

    assert len(usage.events) == 1
    assert usage.events[0].prompt_tokens == 1
    assert usage.events[0].completion_tokens == 3  # 12 streamed chars -> 3 tokens
    assert router_service.usage_calls == [(1, 3)]


async def test_client_disconnect_attaches_estimated_usage() -> None:
    model = _model()
    router = _router(model)
    usage = FakeUsage()
    router_service = RecordingRouter(model)
    service = _service(StreamGateway(_chat_chunks()), usage, router, model, router_service)

    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, dict(REQUEST))
    consumed = 0
    async for _ in stream:
        consumed += 1
        if consumed == 2:  # role chunk + first content chunk
            break
    assert isinstance(stream, AsyncGenerator)
    await stream.aclose()

    assert len(usage.events) == 1
    assert usage.events[0].prompt_tokens == 1
    assert usage.events[0].completion_tokens == 2  # 8 streamed chars -> 2 tokens
    assert router_service.usage_calls == [(1, 2)]


async def test_analytics_attachment_bug_never_breaks_stream_or_billing() -> None:
    model = _model()
    router = _router(model)
    usage = FakeUsage()
    router_service = RaisingRouter(model)
    chunks = [
        *_chat_chunks(),
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7}},
    ]
    service = _service(StreamGateway(chunks), usage, router, model, router_service)

    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, dict(REQUEST))
    received = [chunk async for chunk in stream]

    # The stream still delivers every chunk and billing still settles...
    assert received == chunks
    assert len(usage.events) == 1
    assert usage.events[0].prompt_tokens == 5
    assert usage.events[0].completion_tokens == 7
    # ...even though the analytics attachment itself raised.
    assert router_service.usage_calls == [(5, 7)]
