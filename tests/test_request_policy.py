"""Unit tests for the request parameter allowlist (pure function)."""

from __future__ import annotations

from litestar_gateway.domain.request_policy import (
    MAX_N,
    MAX_TOKENS,
    clamp_output_tokens,
    sanitize_request,
)


def test_drops_transport_and_unknown_keys() -> None:
    request = {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.5,
        # These must never reach the SDK call:
        "extra_headers": {"X-Evil": "1"},
        "extra_body": {"foo": "bar"},
        "extra_query": {"q": "1"},
        "timeout": 999,
        "api_key": "sk-injected",
        "definitely_unknown": True,
    }
    clean = sanitize_request("chat.completions", request)
    assert clean == {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.5,
    }


def test_clamps_cost_drivers() -> None:
    clean = sanitize_request(
        "chat.completions",
        {"model": "m", "messages": [], "n": 1000, "max_tokens": 10**9},
    )
    assert clean["n"] == MAX_N
    assert clean["max_tokens"] == MAX_TOKENS


def test_within_limits_are_untouched() -> None:
    clean = sanitize_request(
        "chat.completions", {"model": "m", "messages": [], "n": 2, "max_tokens": 100}
    )
    assert clean["n"] == 2
    assert clean["max_tokens"] == 100


def test_non_int_cost_values_pass_through() -> None:
    # Type validation is the provider's job; we only clamp real ints.
    clean = sanitize_request("chat.completions", {"model": "m", "n": "lots"})
    assert clean["n"] == "lots"


def test_per_operation_allowlists() -> None:
    # 'input' is valid for embeddings but not chat; 'messages' the reverse.
    assert "input" in sanitize_request("embeddings", {"input": "x", "messages": []})
    assert "messages" not in sanitize_request("embeddings", {"input": "x", "messages": []})
    assert "prompt" in sanitize_request("images", {"prompt": "a cat", "messages": []})
    assert "max_output_tokens" in sanitize_request(
        "responses", {"input": "hi", "max_output_tokens": 10}
    )


def test_does_not_mutate_input() -> None:
    request = {"model": "m", "messages": [], "extra_headers": {"x": "1"}}
    sanitize_request("chat.completions", request)
    assert "extra_headers" in request  # original untouched


def test_clamp_output_tokens_lowers_client_value() -> None:
    assert clamp_output_tokens("chat.completions", {"max_tokens": 5000}, 1000)["max_tokens"] == 1000


def test_clamp_output_tokens_leaves_smaller_client_value() -> None:
    # min semantics: the client may ask for less than the ceiling.
    assert clamp_output_tokens("chat.completions", {"max_tokens": 200}, 1000)["max_tokens"] == 200


def test_clamp_output_tokens_injects_when_client_omits() -> None:
    # Omission must not bypass the cap: inject the operation's canonical field.
    assert clamp_output_tokens("chat.completions", {"messages": []}, 1000)["max_tokens"] == 1000
    assert clamp_output_tokens("responses", {"input": "hi"}, 1000)["max_output_tokens"] == 1000


def test_clamp_output_tokens_noop_without_ceiling() -> None:
    request = {"max_tokens": 10**9}
    assert clamp_output_tokens("chat.completions", request, None) is request


def test_clamp_output_tokens_skips_operations_without_output_tokens() -> None:
    # embeddings/images have no output-token concept: nothing injected.
    assert clamp_output_tokens("embeddings", {"input": "x"}, 1000) == {"input": "x"}
    assert "max_tokens" not in clamp_output_tokens("images", {"prompt": "a cat"}, 1000)


def test_clamp_output_tokens_does_not_mutate_input() -> None:
    request = {"max_tokens": 5000}
    clamp_output_tokens("chat.completions", request, 1000)
    assert request["max_tokens"] == 5000  # original untouched
