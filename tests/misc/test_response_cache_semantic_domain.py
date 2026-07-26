"""Semantic-tier pure domain logic (Plan 04 Phase 2): eligibility, text
extraction, and cosine similarity — no I/O, table-driven where natural.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from litestar_gateway.domain.entities import Model, ModelType, Provider
from litestar_gateway.domain.response_cache_semantic import (
    cosine_similarity,
    extract_semantic_text,
    is_semantic_cacheable,
)

BASE_REQUEST = {
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "hi"}],
    "temperature": 0,
}


def _model(**overrides: object) -> Model:
    defaults: dict[str, object] = dict(
        id=uuid4(),
        team_id=uuid4(),
        name="m",
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gpt-4o",
        params={},
        params_enforced={},
        api_version=None,
        input_cost_per_token=None,
        output_cost_per_token=None,
        enabled=True,
        created_at=datetime.now(UTC),
        cache_enabled=True,
        cache_semantic_enabled=True,
    )
    defaults.update(overrides)
    return Model(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# is_semantic_cacheable — never without exact-match eligibility in front of it.
# ---------------------------------------------------------------------------


def test_semantic_requires_exact_match_eligibility_too() -> None:
    model = _model(cache_enabled=False, cache_semantic_enabled=True)
    assert not is_semantic_cacheable("chat.completions", BASE_REQUEST, model)


def test_semantic_off_even_when_exact_match_is_on() -> None:
    model = _model(cache_enabled=True, cache_semantic_enabled=False)
    assert not is_semantic_cacheable("chat.completions", BASE_REQUEST, model)


def test_semantic_on_when_both_toggles_on() -> None:
    model = _model(cache_enabled=True, cache_semantic_enabled=True)
    assert is_semantic_cacheable("chat.completions", BASE_REQUEST, model)


def test_semantic_still_refuses_sampled_requests_by_default() -> None:
    model = _model(
        cache_enabled=True, cache_semantic_enabled=True, cache_allow_nondeterministic=False
    )
    assert not is_semantic_cacheable(
        "chat.completions", {**BASE_REQUEST, "temperature": 0.7}, model
    )


def test_semantic_only_applies_to_cacheable_operations() -> None:
    model = _model(cache_enabled=True, cache_semantic_enabled=True)
    assert not is_semantic_cacheable("embeddings", BASE_REQUEST, model)


# ---------------------------------------------------------------------------
# extract_semantic_text — a best-effort plain-text view of the request.
# ---------------------------------------------------------------------------


def test_extracts_text_from_a_single_string_message() -> None:
    assert extract_semantic_text({"messages": [{"role": "user", "content": "hello"}]}) == "hello"


def test_extracts_and_joins_multiple_messages() -> None:
    request = {
        "messages": [
            {"role": "system", "content": "be nice"},
            {"role": "user", "content": "hello"},
        ]
    }
    assert extract_semantic_text(request) == "be nice\nhello"


def test_extracts_text_blocks_from_multimodal_content() -> None:
    request = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "http://x"}},
                ],
            }
        ]
    }
    assert extract_semantic_text(request) == "describe this"


def test_returns_none_for_an_all_image_request() -> None:
    request = {
        "messages": [
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://x"}}]}
        ]
    }
    assert extract_semantic_text(request) is None


def test_extracts_text_from_a_string_input_field() -> None:
    assert extract_semantic_text({"input": "hello there"}) == "hello there"


def test_returns_none_when_no_extractable_field_is_present() -> None:
    assert extract_semantic_text({"model": "gpt-4o"}) is None


# ---------------------------------------------------------------------------
# cosine_similarity.
# ---------------------------------------------------------------------------


def test_identical_vectors_have_similarity_one() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0


def test_orthogonal_vectors_have_similarity_zero() -> None:
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_opposite_vectors_have_similarity_negative_one() -> None:
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == -1.0


def test_zero_vector_yields_zero_rather_than_dividing_by_zero() -> None:
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
