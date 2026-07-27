"""Response-cache key derivation and cacheability (Plan 04 Phase 0, design §2/§3/§7).

Table-driven equivalence/difference cases for `derive_cache_key`, plus the
merge-blocking tenant-isolation invariant: `CacheKey` is a frozen dataclass
whose equality includes `team_id`/`api_key_id`, so an identical request body
resolved for a different team or API key can never collide with the original
tenant's key — cross-tenant reuse is not a configuration option (design §3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from litestar_gateway.domain.entities import Model, ModelType, Provider
from litestar_gateway.domain.response_cache_key import derive_cache_key, is_cacheable

TEAM = uuid4()
OTHER_TEAM = uuid4()
KEY = uuid4()
OTHER_KEY = uuid4()

BASE_REQUEST = {
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "hi"}],
    "temperature": 0,
}


def _model(**overrides: object) -> Model:
    defaults: dict[str, object] = dict(
        id=uuid4(),
        team_id=TEAM,
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
    )
    defaults.update(overrides)
    return Model(**defaults)  # type: ignore[arg-type]


MODEL = _model()


# ---------------------------------------------------------------------------
# Key derivation: same body -> same key.
# ---------------------------------------------------------------------------


def test_identical_request_same_model_same_tenant_yields_same_key() -> None:
    a = derive_cache_key(TEAM, KEY, MODEL, "chat.completions", dict(BASE_REQUEST))
    b = derive_cache_key(TEAM, KEY, MODEL, "chat.completions", dict(BASE_REQUEST))
    assert a == b


def test_alias_vs_resolved_canonical_model_name_yields_same_key() -> None:
    # The caller always passes the *resolved* model; the client-supplied `model`
    # field is excluded from the key, so an alias and the model it points at
    # share a hit once resolved identically.
    a = derive_cache_key(TEAM, KEY, MODEL, "chat.completions", dict(BASE_REQUEST))
    b = derive_cache_key(
        TEAM, KEY, MODEL, "chat.completions", {**BASE_REQUEST, "model": "my-alias"}
    )
    assert a.digest == b.digest


def test_stream_and_user_do_not_affect_the_key() -> None:
    base = derive_cache_key(TEAM, KEY, MODEL, "chat.completions", dict(BASE_REQUEST))
    with_stream = derive_cache_key(
        TEAM, KEY, MODEL, "chat.completions", {**BASE_REQUEST, "stream": True, "user": "alice"}
    )
    assert base == with_stream


def test_key_ordering_of_json_object_fields_does_not_affect_the_key() -> None:
    a = derive_cache_key(
        TEAM,
        KEY,
        MODEL,
        "chat.completions",
        {**BASE_REQUEST, "response_format": {"type": "json", "b": 1, "a": 2}},
    )
    b = derive_cache_key(
        TEAM,
        KEY,
        MODEL,
        "chat.completions",
        {**BASE_REQUEST, "response_format": {"a": 2, "b": 1, "type": "json"}},
    )
    assert a == b


def _changed(field: str, value: object) -> None:
    a = derive_cache_key(TEAM, KEY, MODEL, "chat.completions", dict(BASE_REQUEST))
    b = derive_cache_key(TEAM, KEY, MODEL, "chat.completions", {**BASE_REQUEST, field: value})
    assert a != b, f"changing {field!r} must change the key"


def test_changed_temperature_changes_the_key() -> None:
    _changed("temperature", 0.7)


def test_changed_seed_changes_the_key() -> None:
    _changed("seed", 42)


def test_changed_tools_changes_the_key() -> None:
    _changed("tools", [{"type": "function", "function": {"name": "f"}}])


def test_changed_response_format_changes_the_key() -> None:
    _changed("response_format", {"type": "json_object"})


def test_changed_max_tokens_changes_the_key() -> None:
    _changed("max_tokens", 128)


def test_changed_messages_changes_the_key() -> None:
    _changed("messages", [{"role": "user", "content": "bye"}])


# ---------------------------------------------------------------------------
# Tenant isolation — merge-blocking (design §3).
# ---------------------------------------------------------------------------


def test_different_team_id_never_collides_even_with_an_identical_body() -> None:
    mine = derive_cache_key(TEAM, KEY, MODEL, "chat.completions", dict(BASE_REQUEST))
    theirs = derive_cache_key(OTHER_TEAM, KEY, MODEL, "chat.completions", dict(BASE_REQUEST))
    assert mine.digest == theirs.digest  # same canonical body/model
    assert mine != theirs  # but never the same cache entry


def test_different_api_key_id_never_collides_even_within_the_same_team() -> None:
    mine = derive_cache_key(TEAM, KEY, MODEL, "chat.completions", dict(BASE_REQUEST))
    other_key_same_team = derive_cache_key(
        TEAM, OTHER_KEY, MODEL, "chat.completions", dict(BASE_REQUEST)
    )
    assert mine.digest == other_key_same_team.digest
    assert mine != other_key_same_team


def test_no_api_key_never_collides_with_a_keyed_request() -> None:
    keyed = derive_cache_key(TEAM, KEY, MODEL, "chat.completions", dict(BASE_REQUEST))
    keyless = derive_cache_key(TEAM, None, MODEL, "chat.completions", dict(BASE_REQUEST))
    assert keyed != keyless


# ---------------------------------------------------------------------------
# Cacheability gating (design §7).
# ---------------------------------------------------------------------------


def test_only_chat_completions_and_responses_are_cacheable() -> None:
    model = _model(cache_enabled=True)
    assert is_cacheable("chat.completions", BASE_REQUEST, model)
    assert is_cacheable("responses", BASE_REQUEST, model)
    assert not is_cacheable("embeddings", BASE_REQUEST, model)
    assert not is_cacheable("images", BASE_REQUEST, model)
    assert not is_cacheable("native.messages", BASE_REQUEST, model)


def test_model_must_opt_in() -> None:
    model = _model(cache_enabled=False)
    assert not is_cacheable("chat.completions", BASE_REQUEST, model)


def test_sampled_temperature_is_refused_by_default() -> None:
    model = _model(cache_enabled=True, cache_allow_nondeterministic=False)
    assert not is_cacheable("chat.completions", {**BASE_REQUEST, "temperature": 0.7}, model)


def test_sampled_temperature_allowed_when_model_opts_into_nondeterminism() -> None:
    model = _model(cache_enabled=True, cache_allow_nondeterministic=True)
    assert is_cacheable("chat.completions", {**BASE_REQUEST, "temperature": 0.7}, model)


def test_zero_temperature_is_always_cacheable() -> None:
    model = _model(cache_enabled=True, cache_allow_nondeterministic=False)
    assert is_cacheable("chat.completions", {**BASE_REQUEST, "temperature": 0}, model)


def test_absent_temperature_is_cacheable() -> None:
    model = _model(cache_enabled=True, cache_allow_nondeterministic=False)
    request = {k: v for k, v in BASE_REQUEST.items() if k != "temperature"}
    assert is_cacheable("chat.completions", request, model)


# ---------------------------------------------------------------------------
# ISSUE-023a: the key covers the *effective* request and the resolved model.
# ---------------------------------------------------------------------------


def test_a_different_model_id_never_shares_an_entry() -> None:
    # Two models identical in every configured respect still address different
    # deployments (own credential, own history) — and a delete/recreate under
    # the same name must not inherit the old model's answers.
    same_config = _model(id=MODEL.id)
    recreated = _model(name=MODEL.name)
    body = dict(BASE_REQUEST)
    assert derive_cache_key(TEAM, KEY, MODEL, "chat.completions", body) == derive_cache_key(
        TEAM, KEY, same_config, "chat.completions", body
    )
    assert derive_cache_key(TEAM, KEY, MODEL, "chat.completions", body) != derive_cache_key(
        TEAM, KEY, recreated, "chat.completions", body
    )


def test_chat_and_responses_never_share_an_entry() -> None:
    # The reported cross-operation hit: a Responses request must not receive the
    # chat body generated for the same text.
    body = dict(BASE_REQUEST)
    assert derive_cache_key(TEAM, KEY, MODEL, "chat.completions", body) != derive_cache_key(
        TEAM, KEY, MODEL, "responses", body
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("n", 2),
        ("parallel_tool_calls", False),
        ("frequency_penalty", 0.5),
        ("presence_penalty", 0.5),
        ("logit_bias", {"123": -100}),
        ("logprobs", True),
        ("top_logprobs", 5),
        ("reasoning", {"effort": "high"}),
        ("reasoning_effort", "high"),
        ("modalities", ["text", "audio"]),
        ("instructions", "be terse"),
        ("tool_choice", "required"),
        ("service_tier", "flex"),
    ],
)
def test_every_behavior_affecting_field_changes_the_key(field: str, value: object) -> None:
    # The allow-list this replaces ignored all of these: two requests differing
    # only here shared one answer.
    _changed(field, value)


@pytest.mark.parametrize("field", sorted({"stream", "stream_options", "user", "metadata"}))
def test_non_determining_fields_never_change_the_key(field: str) -> None:
    base = derive_cache_key(TEAM, KEY, MODEL, "chat.completions", dict(BASE_REQUEST))
    with_field = derive_cache_key(
        TEAM, KEY, MODEL, "chat.completions", {**BASE_REQUEST, field: {"any": "value"}}
    )
    assert base == with_field


def test_provider_coordinates_are_part_of_the_key() -> None:
    body = dict(BASE_REQUEST)
    base = derive_cache_key(TEAM, KEY, MODEL, "chat.completions", body)
    for field, value in (
        ("provider_model_id", "gpt-4o-mini"),
        ("api_version", "2024-10-01"),
        ("name", "renamed"),
        ("max_output_tokens", 256),
    ):
        variant = _model(id=MODEL.id, **{field: value})
        assert base != derive_cache_key(TEAM, KEY, variant, "chat.completions", body), field


def test_enforced_policy_changes_the_key_through_the_effective_request() -> None:
    # `_cache_key_for` hashes `merge_params(request)`, so tightening a model's
    # enforced policy invalidates entries produced under the old policy.
    before = MODEL.merge_params(dict(BASE_REQUEST))
    after = _model(id=MODEL.id, params_enforced={"tool_choice": "none"}).merge_params(
        dict(BASE_REQUEST)
    )
    assert derive_cache_key(TEAM, KEY, MODEL, "chat.completions", before) != derive_cache_key(
        TEAM, KEY, MODEL, "chat.completions", after
    )
