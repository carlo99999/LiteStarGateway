"""Response caching (Plan 04 Phase 1): Redis-backed store + synthetic-stream
replay, and the Redis failure-policy regression for the streaming path.

Mirrors `tests/completions/test_response_cache.py`'s fake-port style, plus
`tests/completions/test_stream_usage_fallback.py`'s streaming-gateway
pattern, for the three "Done when" behaviors this phase adds:
- a streamed request whose canonical key matches a stored (non-streamed)
  cache entry replays a well-formed synthetic SSE stream and settles the
  tail at $0 via the same `settle_cache_hit` path, without ever calling the
  provider's streaming method;
- a Redis outage (the fake client's get/set raising) falls through to a
  normal, provider-served stream — never a dependency of the money path;
- two separate `CompletionService`/cache-adapter instances sharing one fake
  Redis client see each other's writes (the "shared across replicas"
  property Phase 1 exists to deliver).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.entities import Model, ModelType, Provider, TraceRecord, UsageEvent
from litestar_gateway.infrastructure.cache.redis import RedisResponseCache

TEAM_ID = uuid4()
KEY_ID = uuid4()

_CHAT_REQUEST = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}


class FakeRedis:
    """Same small fake as `tests/misc/test_redis_response_cache.py`."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        self.store[key] = value
        return True


class RaisingRedis:
    """A Redis client double whose every call raises — simulating an outage
    (design §8): a `get`/`set` exception must never fail the request."""

    async def get(self, key: str) -> str | None:
        raise ConnectionError("boom: redis unavailable")

    async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        raise ConnectionError("boom: redis unavailable")


def _model(*, cache_enabled: bool = True) -> Model:
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
        cache_enabled=cache_enabled,
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
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    async def record(self, event: UsageEvent) -> None:
        self.events.append(event)

    async def enqueue_pending(self, event: UsageEvent) -> None:  # pragma: no cover
        raise AssertionError("outbox must not be used in these tests")


class Gateway:
    """Counts non-streamed calls; a streamed call is only ever allowed when
    `allow_stream=True` — the cache-hit test relies on this to prove the
    synthetic replay never opens a real provider stream."""

    def __init__(self, *, allow_stream: bool) -> None:
        self.calls = 0
        self.stream_calls = 0
        self._allow_stream = allow_stream

    async def achat_completion(self, request, model, credentials) -> dict[str, Any]:
        self.calls += 1
        return {
            "id": f"cmpl-{self.calls}",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello world"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        }

    async def astream_chat_completion(
        self, request: Any, model: Any, credentials: Any
    ) -> AsyncIterator[dict[str, Any]]:
        if not self._allow_stream:
            raise AssertionError("the provider stream must never be opened on a cache hit")
        self.stream_calls += 1
        return self._stream()

    async def _stream(self) -> AsyncIterator[dict[str, Any]]:
        yield {"choices": [{"index": 0, "delta": {"role": "assistant"}}]}
        yield {"choices": [{"index": 0, "delta": {"content": "live"}}]}
        yield {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1}}


def _service(
    gateway: Gateway,
    usage: FakeUsage,
    traces: list[TraceRecord],
    *,
    response_cache: object,
    model: Model | None = None,
) -> CompletionService:
    """`model` is explicit for the multi-replica test: two replicas read the
    same model row, so they must be given the same `Model` — the cache key
    includes the model's id (ISSUE-023a)."""
    return CompletionService(
        models=FakeModels(model or _model()),  # type: ignore[arg-type]
        credentials=FakeCredentials(),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        meter=UsageMeter(usage=usage, emit_trace=traces.append),  # type: ignore[arg-type]
        response_cache=response_cache,  # type: ignore[arg-type]
    )


async def _drain(stream: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return [chunk async for chunk in stream]


async def test_a_streamed_request_replays_a_matching_cached_entry_as_a_synthetic_stream() -> None:
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    gateway = Gateway(allow_stream=False)  # any real stream call is a test failure
    cache = RedisResponseCache(FakeRedis())
    service = _service(gateway, usage, traces, response_cache=cache)

    await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))
    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))
    chunks = await _drain(stream)

    assert gateway.calls == 1
    assert gateway.stream_calls == 0  # never opened a real provider stream
    assert chunks  # a well-formed, non-empty synthetic SSE stream
    assert all(chunk.get("object") == "chat.completion.chunk" for chunk in chunks)
    deltas = [c["choices"][0]["delta"] for c in chunks if c.get("choices")]
    assert deltas[0].get("role") == "assistant"
    assert "".join(d.get("content", "") for d in deltas) == "hello world"
    finish_reasons = [c["choices"][0].get("finish_reason") for c in chunks if c.get("choices")]
    assert "stop" in finish_reasons

    assert len(usage.events) == 2  # the seeding call, then the cache-hit settlement
    hit = usage.events[1]
    assert hit.cache_hit is True
    assert hit.cost == 0.0
    assert (hit.prompt_tokens, hit.completion_tokens) == (2, 3)  # the stored counts
    assert [t.cache_hit for t in traces] == [False, True]


async def test_a_redis_outage_falls_through_to_a_normal_streamed_response() -> None:
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    gateway = Gateway(allow_stream=True)
    service = _service(gateway, usage, traces, response_cache=RedisResponseCache(RaisingRedis()))

    stream = await service.open_chat_stream(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))
    chunks = await _drain(stream)

    assert gateway.stream_calls == 1  # fell through to a real provider stream
    content = "".join(
        c["choices"][0]["delta"].get("content", "") for c in chunks if c.get("choices")
    )
    assert content == "live"
    assert len(usage.events) == 1
    assert usage.events[0].cache_hit is False
    assert (usage.events[0].prompt_tokens, usage.events[0].completion_tokens) == (5, 1)


async def test_two_service_instances_sharing_one_fake_redis_client_share_hits() -> None:
    """The "shared across replicas" property: instance 1 writes (a non-streamed
    call), instance 2 — a wholly separate `CompletionService`/cache-adapter
    pair standing in for a second replica — reads the hit."""
    client = FakeRedis()
    model = _model()  # the one model row both replicas resolve
    traces_1: list[TraceRecord] = []
    usage_1 = FakeUsage()
    gateway_1 = Gateway(allow_stream=True)
    service_1 = _service(
        gateway_1, usage_1, traces_1, response_cache=RedisResponseCache(client), model=model
    )

    traces_2: list[TraceRecord] = []
    usage_2 = FakeUsage()
    gateway_2 = Gateway(allow_stream=False)
    service_2 = _service(
        gateway_2, usage_2, traces_2, response_cache=RedisResponseCache(client), model=model
    )

    await service_1.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))
    stream = await service_2.open_chat_stream(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))
    chunks = await _drain(stream)

    assert gateway_2.stream_calls == 0  # replica 2 served the hit written by replica 1
    assert chunks
    assert len(usage_2.events) == 1
    assert usage_2.events[0].cache_hit is True
