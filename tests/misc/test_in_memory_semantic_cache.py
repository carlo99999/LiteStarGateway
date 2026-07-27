"""InMemorySemanticResponseCache — threshold matching, TTL, tenant isolation,
and bounded per-tenant size (Plan 04 Phase 2)."""

from __future__ import annotations

from uuid import uuid4

from litestar_gateway.domain.ports.response_cache import CachedResponse, SemanticScope
from litestar_gateway.infrastructure.cache.semantic import InMemorySemanticResponseCache

TEAM = uuid4()
OTHER_TEAM = uuid4()
KEY = uuid4()
OTHER_KEY = uuid4()
MODEL = uuid4()
OTHER_MODEL = uuid4()


def _scope(
    team=TEAM, api_key=KEY, model=MODEL, operation="chat.completions", digest="d0"
) -> SemanticScope:
    return SemanticScope(
        team_id=team,
        api_key_id=api_key,
        model_id=model,
        operation=operation,
        request_digest=digest,
    )


SCOPE = _scope()


def _value(tag: str) -> CachedResponse:
    return CachedResponse(body={"id": tag}, prompt_tokens=1, completion_tokens=2)


async def test_a_vector_at_the_threshold_hits() -> None:
    cache = InMemorySemanticResponseCache()
    await cache.add(SCOPE, [1.0, 0.0], _value("a"), ttl_s=60)

    found = await cache.find(SCOPE, [1.0, 0.0], threshold=0.97)

    assert found == _value("a")


async def test_a_vector_below_threshold_misses() -> None:
    cache = InMemorySemanticResponseCache()
    await cache.add(SCOPE, [1.0, 0.0], _value("a"), ttl_s=60)

    # Orthogonal vector: cosine similarity 0.0, well below any sane threshold.
    found = await cache.find(SCOPE, [0.0, 1.0], threshold=0.97)

    assert found is None


async def test_a_different_team_never_matches_even_with_an_identical_vector() -> None:
    cache = InMemorySemanticResponseCache()
    await cache.add(SCOPE, [1.0, 0.0], _value("a"), ttl_s=60)

    found = await cache.find(_scope(team=OTHER_TEAM), [1.0, 0.0], threshold=0.97)

    assert found is None  # identical vector, similarity 1.0, still a miss


async def test_a_different_api_key_never_matches_within_the_same_team() -> None:
    cache = InMemorySemanticResponseCache()
    await cache.add(SCOPE, [1.0, 0.0], _value("a"), ttl_s=60)

    found = await cache.find(_scope(api_key=OTHER_KEY), [1.0, 0.0], threshold=0.97)

    assert found is None


async def test_an_expired_entry_is_a_miss() -> None:
    clock_value = [0.0]
    cache = InMemorySemanticResponseCache(clock=lambda: clock_value[0])
    await cache.add(SCOPE, [1.0, 0.0], _value("a"), ttl_s=60)
    clock_value[0] = 61.0

    assert await cache.find(SCOPE, [1.0, 0.0], threshold=0.97) is None


async def test_bucket_is_bounded_per_tenant() -> None:
    cache = InMemorySemanticResponseCache(max_entries_per_tenant=2)
    await cache.add(SCOPE, [1.0, 0.0, 0.0], _value("a"), ttl_s=60)
    await cache.add(SCOPE, [0.0, 1.0, 0.0], _value("b"), ttl_s=60)
    await cache.add(SCOPE, [0.0, 0.0, 1.0], _value("c"), ttl_s=60)

    # The oldest ("a") was evicted; only "b" and "c" remain findable.
    assert await cache.find(SCOPE, [1.0, 0.0, 0.0], threshold=0.5) is None
    assert await cache.find(SCOPE, [0.0, 0.0, 1.0], threshold=0.99) == _value("c")


async def test_the_closest_entry_above_threshold_wins() -> None:
    cache = InMemorySemanticResponseCache()
    await cache.add(SCOPE, [1.0, 0.1], _value("closer"), ttl_s=60)
    await cache.add(SCOPE, [1.0, 0.5], _value("farther"), ttl_s=60)

    found = await cache.find(SCOPE, [1.0, 0.0], threshold=0.5)

    assert found == _value("closer")


# ---------------------------------------------------------------------------
# ISSUE-023b: similarity may only blur the text, never the scope.
# ---------------------------------------------------------------------------


async def test_a_different_model_never_matches_an_identical_vector() -> None:
    cache = InMemorySemanticResponseCache()
    await cache.add(SCOPE, [1.0, 0.0], _value("a"), ttl_s=60)

    found = await cache.find(_scope(model=OTHER_MODEL), [1.0, 0.0], threshold=0.97)

    assert found is None  # similarity 1.0, different model, still a miss


async def test_a_different_operation_never_matches_an_identical_vector() -> None:
    cache = InMemorySemanticResponseCache()
    await cache.add(SCOPE, [1.0, 0.0], _value("a"), ttl_s=60)

    found = await cache.find(_scope(operation="responses"), [1.0, 0.0], threshold=0.97)

    assert found is None


async def test_a_different_request_contract_never_matches_an_identical_vector() -> None:
    # Different instructions/tools/output format ⇒ different digest ⇒ no reuse,
    # even though the embedded text is byte-identical.
    cache = InMemorySemanticResponseCache()
    await cache.add(SCOPE, [1.0, 0.0], _value("a"), ttl_s=60)

    found = await cache.find(_scope(digest="d1"), [1.0, 0.0], threshold=0.97)

    assert found is None
