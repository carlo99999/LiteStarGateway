"""Response caching (Plan 04 Phase 2): the semantic tier.

Unit tests for `CompletionService` with fake ports (mirrors
`test_response_cache.py`), covering the plan's TDD strategy:
- a near-duplicate prompt at/above threshold hits on an exact-match miss;
- a below-threshold prompt misses (falls through to the provider);
- a cross-tenant near-duplicate (even similarity 1.0) always misses — the
  same hard tenant-isolation invariant as the exact-match tier;
- semantic opted out ⇒ no semantic lookup at all, even on an exact miss
  (asserted via a spy counter on the embedding call);
- a semantic hit settles at $0 with the *stored* token counts, not the new
  request's.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.entities import Model, ModelType, Provider, TraceRecord, UsageEvent
from litestar_gateway.infrastructure.cache.in_memory import InMemoryResponseCache
from litestar_gateway.infrastructure.cache.semantic import InMemorySemanticResponseCache

TEAM_ID = uuid4()
OTHER_TEAM_ID = uuid4()
KEY_ID = uuid4()

EMBED_MODEL_NAME = "text-embedding-3-small"

_CHAT_REQUEST = {
    "model": "m",
    "messages": [{"role": "user", "content": "hi there"}],
    "temperature": 0,
}
_NEAR_DUP_REQUEST = {
    "model": "m",
    "messages": [{"role": "user", "content": "hi there!"}],  # different exact body
    "temperature": 0,
}


def _model(
    *,
    cache_enabled: bool = True,
    cache_semantic_enabled: bool = True,
    cache_allow_nondeterministic: bool = False,
) -> Model:
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
        cache_semantic_enabled=cache_semantic_enabled,
    )


def _embed_model() -> Model:
    return Model(
        id=uuid4(),
        team_id=TEAM_ID,
        name=EMBED_MODEL_NAME,
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=ModelType.EMBEDDINGS,
        provider_model_id="text-embedding-3-small",
        params={},
        api_version=None,
        input_cost_per_token=None,
        output_cost_per_token=None,
        enabled=True,
        created_at=datetime.now(UTC),
    )


class FakeModels:
    """Resolves the chat model by its name, and the embedding model by its
    own name — `team_id` is otherwise ignored (mirrors `test_response_cache.py`
    so the tenant-isolation test exercises the same model/request across two
    different teams)."""

    def __init__(self, model: Model, embed_model: Model | None = None) -> None:
        self._model = model
        self._embed_model = embed_model

    async def get_by_name(self, team_id: UUID, name: str) -> Model | None:
        if name == self._model.name:
            return self._model
        if self._embed_model is not None and name == self._embed_model.name:
            return self._embed_model
        return None


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


class FakeEmbeddingGateway:
    """Counts real provider calls (chat completions and embeddings
    separately); `achat_completion` returns fresh, authoritative usage,
    `aembeddings` returns a vector looked up from `vectors_by_text` (falling
    back to a fixed default) so tests can control similarity precisely."""

    def __init__(self, vectors_by_text: dict[str, list[float]] | None = None) -> None:
        self.chat_calls = 0
        self.embed_calls = 0
        self._vectors_by_text = vectors_by_text or {}

    async def achat_completion(self, request, model, credentials) -> dict[str, Any]:
        self.chat_calls += 1
        return {
            "id": f"cmpl-{self.chat_calls}",
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        }

    async def aembeddings(self, request, model, credentials) -> dict[str, Any]:
        self.embed_calls += 1
        text = request["input"][0]
        vector = self._vectors_by_text.get(text, [1.0, 0.0])
        return {"data": [{"embedding": vector}]}


def _service(
    gateway: FakeEmbeddingGateway,
    usage: FakeUsage,
    traces: list[TraceRecord],
    *,
    model: Model | None = None,
    semantic_cache: object | None = None,
    embedding_model: Model | None = None,
    semantic_threshold: float = 0.97,
    semantic_embedding_model: str | None = EMBED_MODEL_NAME,
) -> CompletionService:
    return CompletionService(
        models=FakeModels(model or _model(), embedding_model or _embed_model()),  # type: ignore[arg-type]
        credentials=FakeCredentials(),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        meter=UsageMeter(usage=usage, emit_trace=traces.append),  # type: ignore[arg-type]
        response_cache=InMemoryResponseCache(max_entries=8),
        semantic_cache=semantic_cache,  # type: ignore[arg-type]
        semantic_threshold=semantic_threshold,
        semantic_embedding_model=semantic_embedding_model,
    )


async def test_a_near_duplicate_prompt_at_or_above_threshold_hits() -> None:
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    # Both requests embed to (nearly) the same vector: cosine similarity 1.0.
    gateway = FakeEmbeddingGateway({"hi there": [1.0, 0.0], "hi there!": [1.0, 0.0]})
    service = _service(gateway, usage, traces, semantic_cache=InMemorySemanticResponseCache())

    first = await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))
    second = await service.chat_completion(TEAM_ID, KEY_ID, dict(_NEAR_DUP_REQUEST))

    assert gateway.chat_calls == 1  # the second, near-duplicate request never hit the provider
    assert second == first
    hit = usage.events[1]
    assert hit.cache_hit is True
    assert hit.cost == 0.0


async def test_a_below_threshold_prompt_misses() -> None:
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    # Orthogonal vectors: cosine similarity 0.0, well below the default 0.97.
    gateway = FakeEmbeddingGateway({"hi there": [1.0, 0.0], "hi there!": [0.0, 1.0]})
    service = _service(gateway, usage, traces, semantic_cache=InMemorySemanticResponseCache())

    await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))
    await service.chat_completion(TEAM_ID, KEY_ID, dict(_NEAR_DUP_REQUEST))

    assert gateway.chat_calls == 2  # both requests hit the provider


async def test_cross_tenant_near_duplicate_misses_regardless_of_similarity() -> None:
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    # Identical vector (similarity 1.0) — still must miss across tenants.
    gateway = FakeEmbeddingGateway({"hi there": [1.0, 0.0], "hi there!": [1.0, 0.0]})
    service = _service(gateway, usage, traces, semantic_cache=InMemorySemanticResponseCache())

    await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))
    await service.chat_completion(OTHER_TEAM_ID, KEY_ID, dict(_NEAR_DUP_REQUEST))

    assert gateway.chat_calls == 2  # never served from the first team's semantic entry


async def test_semantic_opted_out_never_embeds_even_on_an_exact_miss() -> None:
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    gateway = FakeEmbeddingGateway()
    service = _service(
        gateway,
        usage,
        traces,
        model=_model(cache_semantic_enabled=False),
        semantic_cache=InMemorySemanticResponseCache(),
    )

    await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))
    await service.chat_completion(TEAM_ID, KEY_ID, dict(_NEAR_DUP_REQUEST))

    assert gateway.chat_calls == 2  # both missed (different exact keys)
    assert gateway.embed_calls == 0  # semantic tier was never even attempted


async def test_semantic_cache_none_never_embeds_even_when_model_opted_in() -> None:
    """The global semantic kill-switch (`semantic_cache=None`) must be as
    inert as `response_cache=None` is for the exact-match tier."""
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    gateway = FakeEmbeddingGateway()
    service = _service(gateway, usage, traces, semantic_cache=None)

    await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))
    await service.chat_completion(TEAM_ID, KEY_ID, dict(_NEAR_DUP_REQUEST))

    assert gateway.chat_calls == 2
    assert gateway.embed_calls == 0


async def test_a_semantic_hit_settles_at_zero_cost_with_the_stored_token_counts() -> None:
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    gateway = FakeEmbeddingGateway({"hi there": [1.0, 0.0], "hi there!": [1.0, 0.0]})
    service = _service(gateway, usage, traces, semantic_cache=InMemorySemanticResponseCache())

    await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))
    await service.chat_completion(TEAM_ID, KEY_ID, dict(_NEAR_DUP_REQUEST))

    assert len(usage.events) == 2
    hit = usage.events[1]
    assert hit.cache_hit is True
    assert hit.cost == 0.0
    # The stored (first request's) counts, never re-derived from the second.
    assert (hit.prompt_tokens, hit.completion_tokens) == (2, 3)
    assert [t.cache_hit for t in traces] == [False, True]


async def test_exact_match_is_still_tried_first_and_skips_semantic_lookup_on_a_hit() -> None:
    """An exact repeat is served by the exact-match tier alone: the first
    (miss) request embeds once to populate the semantic tier's write path,
    but the second, identical request never needs a semantic *lookup* — it
    hits exact-match and short-circuits before `_semantic_get` runs."""
    traces: list[TraceRecord] = []
    usage = FakeUsage()
    gateway = FakeEmbeddingGateway()
    service = _service(gateway, usage, traces, semantic_cache=InMemorySemanticResponseCache())

    await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))
    embeds_after_first_request = gateway.embed_calls
    await service.chat_completion(TEAM_ID, KEY_ID, dict(_CHAT_REQUEST))

    assert gateway.chat_calls == 1
    assert gateway.embed_calls == embeds_after_first_request  # no further embed on the exact hit
