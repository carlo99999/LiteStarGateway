"""Response caching (Plan 04 Phase 0): exact-match, in-memory, off by default.

Unit tests for `CompletionService` with fake ports (mirrors `test_traces.py`),
covering the plan's TDD strategy for the integration/regression/off tiers:
- a hit skips the provider and settles a `cache_hit` usage event at $0 with the
  stored token counts (the "Done when" acceptance test, design §6);
- a cache adapter that raises on `get`/`put` still yields a normal,
  provider-served response (design §8 — never a dependency of the money path);
- the global kill-switch off (`response_cache=None`) is byte-identical to
  today: no lookup, no write.

Tenant isolation (design §3, the merge-blocking invariant) is the pure unit
test in `tests/misc/test_response_cache_key.py`, per the plan's own TDD
strategy; this module adds one end-to-end confirmation through the service.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.application.guardrails.service import ChainedProvider
from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.entities import Model, ModelType, Provider, TraceRecord, UsageEvent
from litestar_gateway.domain.exceptions import GuardrailBlocked
from litestar_gateway.domain.guardrails import (
    Decision,
    Direction,
    FailPolicy,
    GuardrailPayload,
    GuardrailVerdict,
)
from litestar_gateway.domain.ports.response_cache import CachedResponse, CacheKey
from litestar_gateway.infrastructure.cache.in_memory import InMemoryResponseCache

TEAM_ID = uuid4()
OTHER_TEAM_ID = uuid4()
KEY_ID = uuid4()

_CHAT_REQUEST = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}


def _model(*, cache_enabled: bool = True, cache_allow_nondeterministic: bool = False) -> Model:
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
        cache_allow_nondeterministic=cache_allow_nondeterministic,
    )


class FakeModels:
    """Every team resolves the same model by name — `team_id` is otherwise
    ignored, so the tenant-isolation test below exercises `_dispatch`'s cache
    lookup for two different teams against the identical model/request."""

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


class CountingGateway:
    """Counts real provider calls; each call returns fresh, authoritative usage."""

    def __init__(self) -> None:
        self.calls = 0

    async def achat_completion(self, request, model, credentials) -> dict[str, Any]:
        self.calls += 1
        return {
            "id": f"cmpl-{self.calls}",
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        }


class RaisingCache:
    """A `ResponseCache` whose every call raises — the failure-policy
    regression: a cache exception must never fail the request (design §8)."""

    async def get(self, key: CacheKey) -> CachedResponse | None:
        raise RuntimeError("boom: cache backend unavailable")

    async def put(self, key: CacheKey, value: CachedResponse, ttl_s: int) -> None:
        raise RuntimeError("boom: cache backend unavailable")


class _ResponseGuardrail:
    """A RESPONSE-direction provider that rewrites — or refuses — the answer.

    What the cache stores has to be what the chain produced: a stored raw body
    is served to the next identical request without the chain running again.
    """

    def __init__(self, needle: str, replacement: str = "", *, blocks: bool = False) -> None:
        self.name = "response-guard"
        self._needle = needle
        self._replacement = replacement
        self._blocks = blocks

    def supports(self, direction: Direction) -> bool:
        return direction is Direction.RESPONSE

    async def check(self, payload: GuardrailPayload) -> GuardrailVerdict:
        if self._needle not in payload.text:
            return GuardrailVerdict(decision=Decision.ALLOW, provider=self.name)
        if self._blocks:
            return GuardrailVerdict(
                decision=Decision.BLOCK, provider=self.name, categories=("policy",)
            )
        return GuardrailVerdict(
            decision=Decision.REDACT,
            provider=self.name,
            categories=("policy",),
            counts={"policy": 1},
            redacted_text=payload.text.replace(self._needle, self._replacement),
        )


def _response_chain(provider: _ResponseGuardrail) -> Any:
    chain = (ChainedProvider(provider=provider, fail=FailPolicy.CLOSED),)  # type: ignore[arg-type]

    async def resolver(
        team_id: UUID,
        api_key_id: UUID | None,
        model: Model,
        direction: Direction,
        router_id: UUID | None = None,
    ) -> Any:
        return chain if direction is Direction.RESPONSE else ()

    return resolver


def _service(
    gateway: CountingGateway,
    usage: FakeUsage,
    traces: list[TraceRecord],
    *,
    model: Model | None = None,
    response_cache: object | None = None,
    guardrails: Any = None,
) -> CompletionService:
    return CompletionService(
        models=FakeModels(model or _model()),  # type: ignore[arg-type]
        credentials=FakeCredentials(),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        meter=UsageMeter(usage=usage, emit_trace=traces.append),  # type: ignore[arg-type]
        response_cache=response_cache,  # type: ignore[arg-type]
        guardrails=guardrails,
    )


async def test_repeated_identical_request_hits_the_cache_and_skips_the_provider() -> None:
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    gateway = CountingGateway()
    service = _service(gateway, usage, traces, response_cache=InMemoryResponseCache(max_entries=8))

    first = await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))
    second = await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))

    assert gateway.calls == 1  # the provider was never called again
    assert second == first  # the stored body is returned verbatim
    assert len(usage.events) == 2
    hit = usage.events[1]
    assert hit.cache_hit is True
    assert hit.cost == 0.0
    assert (hit.prompt_tokens, hit.completion_tokens) == (2, 3)  # stored counts, not re-billed
    assert [t.cache_hit for t in traces] == [False, True]


async def test_cache_disabled_globally_is_byte_identical_to_today() -> None:
    """`response_cache=None` (the global RESPONSE_CACHE_ENABLED kill-switch
    off) performs no lookup and no write — every request hits the provider."""
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    gateway = CountingGateway()
    service = _service(gateway, usage, traces, response_cache=None)

    await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))
    await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))

    assert gateway.calls == 2
    assert len(usage.events) == 2
    assert not any(event.cache_hit for event in usage.events)


async def test_model_without_cache_opt_in_never_participates() -> None:
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    gateway = CountingGateway()
    service = _service(
        gateway,
        usage,
        traces,
        model=_model(cache_enabled=False),
        response_cache=InMemoryResponseCache(max_entries=8),
    )

    await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))
    await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))

    assert gateway.calls == 2  # the global switch is on, but the model never opted in


async def test_sampled_request_is_not_cached_without_explicit_opt_in() -> None:
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    gateway = CountingGateway()
    service = _service(gateway, usage, traces, response_cache=InMemoryResponseCache(max_entries=8))
    sampled = {**_CHAT_REQUEST, "temperature": 0.7}

    await service.chat_completion(TEAM_ID, KEY_ID, dict(sampled))
    await service.chat_completion(TEAM_ID, KEY_ID, dict(sampled))

    assert gateway.calls == 2  # temperature > 0 refuses caching by default


async def test_a_raising_cache_still_yields_a_normal_provider_served_response() -> None:
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    gateway = CountingGateway()
    service = _service(gateway, usage, traces, response_cache=RaisingCache())

    response = await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))

    assert gateway.calls == 1
    assert response["choices"][0]["message"]["content"] == "hello"
    assert len(usage.events) == 1
    assert usage.events[0].cache_hit is False
    assert (usage.events[0].prompt_tokens, usage.events[0].completion_tokens) == (2, 3)


async def test_a_different_team_never_gets_a_cached_response_for_the_same_body() -> None:
    """End-to-end confirmation of the tenant-isolation invariant (design §3)
    through the service, on top of the pure key-derivation unit test."""
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    gateway = CountingGateway()
    service = _service(gateway, usage, traces, response_cache=InMemoryResponseCache(max_entries=8))

    await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))
    await service.chat_completion(OTHER_TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))

    assert gateway.calls == 2  # never served from the first team's cache entry


# ---------------------------------------------------------------------------
# ISSUE-023a: an entry belongs to one model, one operation, one policy.
# ---------------------------------------------------------------------------


class MultiModelGateway(CountingGateway):
    """Also serves the Responses API, so a chat entry and a Responses entry for
    the same text can be told apart by which method produced the body."""

    async def aresponses(self, request, model, credentials) -> dict[str, Any]:
        self.calls += 1
        return {
            "id": f"resp-{self.calls}",
            "output": [{"content": [{"type": "output_text", "text": "hello"}]}],
            "usage": {"input_tokens": 2, "output_tokens": 3},
        }


class NamedModels:
    """Resolves each model by its own name, so one service can serve two."""

    def __init__(self, *models: Model) -> None:
        self._by_name = {m.name: m for m in models}

    async def get_by_name(self, team_id: UUID, name: str) -> Model | None:
        return self._by_name.get(name)


def _named_model(name: str, **overrides: Any) -> Model:
    base = _model()
    return replace(base, id=uuid4(), name=name, **overrides)


def _multi_service(
    gateway: CountingGateway, usage: FakeUsage, traces: list[TraceRecord], *models: Model
) -> CompletionService:
    return CompletionService(
        models=NamedModels(*models),  # type: ignore[arg-type]
        credentials=FakeCredentials(),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        meter=UsageMeter(usage=usage, emit_trace=traces.append),  # type: ignore[arg-type]
        response_cache=InMemoryResponseCache(max_entries=8),
    )


async def test_a_second_model_never_serves_the_first_models_cached_body() -> None:
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    gateway = MultiModelGateway()
    a, b = _named_model("model-a"), _named_model("model-b")
    service = _multi_service(gateway, usage, traces, a, b)

    first = await service.chat_completion(TEAM_ID, KEY_ID, {**_CHAT_REQUEST, "model": "model-a"})
    second = await service.chat_completion(TEAM_ID, KEY_ID, {**_CHAT_REQUEST, "model": "model-b"})

    assert gateway.calls == 2
    assert first["id"] != second["id"]


async def test_responses_never_serves_a_chat_cached_body() -> None:
    # The reported cross-operation hit: same text, same model, different API.
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    gateway = MultiModelGateway()
    model = _named_model("model-a")
    service = _multi_service(gateway, usage, traces, model)

    chat = await service.chat_completion(TEAM_ID, KEY_ID, {**_CHAT_REQUEST, "model": "model-a"})
    responses = await service.responses(
        TEAM_ID, KEY_ID, {"model": "model-a", "input": "hi", "temperature": 0}
    )

    assert gateway.calls == 2
    assert "choices" in chat and "output" in responses


async def test_a_behavior_affecting_field_outside_the_old_allow_list_misses() -> None:
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    gateway = MultiModelGateway()
    service = _multi_service(gateway, usage, traces, _named_model("model-a"))

    base = {**_CHAT_REQUEST, "model": "model-a"}
    await service.chat_completion(TEAM_ID, KEY_ID, dict(base))
    await service.chat_completion(TEAM_ID, KEY_ID, {**base, "parallel_tool_calls": False})
    await service.chat_completion(TEAM_ID, KEY_ID, {**base, "frequency_penalty": 0.5})

    assert gateway.calls == 3


async def test_tightening_enforced_policy_invalidates_earlier_entries() -> None:
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    gateway = MultiModelGateway()
    before = _named_model("model-a")
    service = _multi_service(gateway, usage, traces, before)
    await service.chat_completion(TEAM_ID, KEY_ID, {**_CHAT_REQUEST, "model": "model-a"})

    after = replace(before, params_enforced={"tool_choice": "none"})
    reconfigured = _multi_service(gateway, usage, traces, after)
    reconfigured._response_cache = service._response_cache  # same shared store
    await reconfigured.chat_completion(TEAM_ID, KEY_ID, {**_CHAT_REQUEST, "model": "model-a"})

    assert gateway.calls == 2


async def test_the_cache_stores_the_screened_response_not_the_raw_one() -> None:
    """ISSUE-036: `_cache_put` ran before `_guard_response`, so the stored body
    was the provider's raw answer. The caller of the first request got a
    screened response and every later identical request got the unscreened one
    straight out of the cache, with the chain never consulted."""
    traces: list[TraceRecord] = []
    gateway = CountingGateway()
    service = _service(
        gateway,
        FakeUsage(),
        traces,
        response_cache=InMemoryResponseCache(max_entries=8),
        guardrails=_response_chain(_ResponseGuardrail("hello", "[MASKED]")),
    )

    first = await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))
    second = await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))

    assert gateway.calls == 1  # the second request was served from the cache
    assert first["choices"][0]["message"]["content"] == "[MASKED]"
    assert second == first  # ...and what it served was screened, not the raw body


async def test_a_blocked_response_never_reaches_the_cache() -> None:
    """The sharper version of the same defect: a refused answer was stored
    first, so the next identical request was handed the very content the
    guardrail had just refused — and handed it as a 200."""
    gateway = CountingGateway()
    service = _service(
        gateway,
        FakeUsage(),
        [],
        response_cache=InMemoryResponseCache(max_entries=8),
        guardrails=_response_chain(_ResponseGuardrail("hello", blocks=True)),
    )

    with pytest.raises(GuardrailBlocked):
        await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))
    with pytest.raises(GuardrailBlocked):
        await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))

    # Two provider calls: the second request found nothing cached to serve.
    assert gateway.calls == 2
