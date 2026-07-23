"""Unit tests for the request parameter allowlist (pure function)."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from openai.resources.responses.responses import AsyncResponses

from litestar_gateway.domain.chat_tool_policy import (
    MAX_TOOL_COUNT,
    MAX_TOOL_JSON_DEPTH,
    validate_chat_request,
)
from litestar_gateway.domain.entities import Model, ModelType, Provider
from litestar_gateway.domain.exceptions import UnsupportedOperation
from litestar_gateway.domain.request_policy import (
    MAX_N,
    MAX_TOKENS,
    clamp_output_tokens,
    sanitize_request,
    validate_responses_request,
)


def _model(
    provider: Provider,
    *,
    params: dict[str, object] | None = None,
    params_enforced: dict[str, object] | None = None,
    provider_model_id: str = "upstream",
) -> Model:
    return Model(
        id=uuid4(),
        team_id=uuid4(),
        name="m",
        provider=provider,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id=provider_model_id,
        params=params or {},
        params_enforced=params_enforced or {},
        api_version=None,
        input_cost_per_token=None,
        output_cost_per_token=None,
        enabled=True,
        created_at=datetime.now(UTC),
    )


def _validate_responses_request(
    provider: Provider,
    request: dict[str, object],
    *,
    params: dict[str, object] | None = None,
    params_enforced: dict[str, object] | None = None,
    provider_model_id: str = "upstream",
) -> dict[str, object]:
    return validate_responses_request(
        _model(
            provider,
            params=params,
            params_enforced=params_enforced,
            provider_model_id=provider_model_id,
        ),
        request,
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


def test_responses_allowlist_matches_native_sdk_body_fields() -> None:
    transport_fields = {"self", "extra_headers", "extra_query", "extra_body", "timeout"}
    sdk_body_fields = set(inspect.signature(AsyncResponses.create).parameters) - transport_fields
    request = {field: object() for field in sdk_body_fields}

    clean = sanitize_request("responses", request)

    assert set(clean) == sdk_body_fields
    assert all(field not in clean for field in transport_fields)


@pytest.mark.parametrize(
    "field,value",
    [
        ("reasoning", {"effort": "medium"}),
        ("metadata", {"tenant": "acme"}),
        ("store", True),
        ("previous_response_id", "resp_123"),
        ("user", "user_123"),
        ("background", True),
        ("include", ["reasoning.encrypted_content"]),
    ],
)
def test_chat_only_responses_reject_fields_the_emulator_drops(field: str, value: object) -> None:
    with pytest.raises(UnsupportedOperation, match=field):
        _validate_responses_request(
            Provider.DATABRICKS,
            {"model": "m", "input": "hi", field: value},
        )


def test_databricks_responses_accept_non_streaming_function_tool_subset() -> None:
    request: dict[str, object] = {
        "model": "m",
        "input": [
            {"role": "user", "content": "weather in Paris?"},
            {
                "id": "fc_call_123",
                "type": "function_call",
                "status": "completed",
                "call_id": "call_123",
                "name": "get_weather",
                "arguments": '{"city":"Paris"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_123",
                "output": "18C and sunny",
            },
        ],
        "tools": [
            {
                "type": "function",
                "name": "get_weather",
                "description": "Get weather for a city.",
                "parameters": {"type": "object"},
                "strict": True,
            }
        ],
        "tool_choice": {"type": "function", "name": "get_weather"},
        "parallel_tool_calls": False,
        "stream": False,
    }

    assert _validate_responses_request(Provider.DATABRICKS, request) == request


@pytest.mark.parametrize("choice", ["auto", "none", "required"])
def test_databricks_responses_accept_string_tool_choices(choice: str) -> None:
    request: dict[str, object] = {
        "model": "m",
        "input": "weather?",
        "tools": [
            {
                "type": "function",
                "name": "get_weather",
                "parameters": {"type": "object"},
                "strict": False,
            }
        ],
        "tool_choice": choice,
    }

    assert _validate_responses_request(Provider.DATABRICKS, request) == request


def test_vertex_responses_still_reject_function_tools() -> None:
    with pytest.raises(UnsupportedOperation, match="tools"):
        _validate_responses_request(
            Provider.VERTEX_AI,
            {
                "model": "m",
                "input": "weather?",
                "tools": [
                    {
                        "type": "function",
                        "name": "get_weather",
                        "parameters": {"type": "object"},
                        "strict": False,
                    }
                ],
            },
        )


def test_bedrock_responses_accepts_non_streaming_function_tool_subset() -> None:
    request: dict[str, object] = {
        "model": "m",
        "input": [
            {
                "type": "function_call",
                "call_id": "call_123",
                "name": "get_weather",
                "arguments": '{"city":"Rome"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_123",
                "output": "sunny",
            },
        ],
        "tools": [
            {
                "type": "function",
                "name": "get_weather",
                "parameters": {"type": "object"},
                "strict": False,
            }
        ],
        "tool_choice": "required",
        "parallel_tool_calls": True,
    }

    assert (
        _validate_responses_request(
            Provider.BEDROCK,
            request,
            provider_model_id="anthropic.claude-3-5-sonnet-v2:0",
        )
        == request
    )


@pytest.mark.parametrize(
    ("provider_model_id", "extra", "match"),
    [
        (
            "anthropic.claude-3-5-sonnet-v2:0",
            {"tool_choice": "none"},
            "tool_choice='none'",
        ),
        (
            "anthropic.claude-3-5-sonnet-v2:0",
            {"parallel_tool_calls": False},
            "parallel_tool_calls=false",
        ),
        (
            "meta.llama3-70b-instruct-v1:0",
            {},
            "validated",
        ),
    ],
)
def test_bedrock_responses_rejects_unsupported_tool_capabilities(
    provider_model_id: str,
    extra: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(UnsupportedOperation, match=match):
        _validate_responses_request(
            Provider.BEDROCK,
            {
                "model": "m",
                "input": "weather?",
                "tools": [
                    {
                        "type": "function",
                        "name": "get_weather",
                        "parameters": {"type": "object"},
                    }
                ],
                **extra,
            },
            provider_model_id=provider_model_id,
        )


def test_anthropic_responses_accept_non_streaming_function_tool_subset() -> None:
    request: dict[str, object] = {
        "model": "m",
        "input": "weather?",
        "tools": [
            {
                "type": "function",
                "name": "get_weather",
                "parameters": {"type": "object"},
                "strict": True,
            }
        ],
        "tool_choice": "required",
        "parallel_tool_calls": False,
    }

    assert _validate_responses_request(Provider.ANTHROPIC, request) == request


@pytest.mark.parametrize(
    "payload",
    [
        {
            "model": "m",
            "input": "weather?",
            "tools": [],
        },
        {
            "model": "m",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_123",
                    "name": "undeclared",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_123",
                    "output": "sunny",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "get_weather",
                    "parameters": {"type": "object"},
                }
            ],
        },
    ],
)
def test_anthropic_responses_requires_declared_tools_for_every_tool_request(
    payload: dict[str, object],
) -> None:
    with pytest.raises(UnsupportedOperation, match="declared|non-empty"):
        _validate_responses_request(Provider.ANTHROPIC, payload)


def test_anthropic_responses_rejects_configured_structured_output_with_client_tools() -> None:
    with pytest.raises(UnsupportedOperation, match="response_format.*client tools"):
        _validate_responses_request(
            Provider.ANTHROPIC,
            {
                "model": "m",
                "input": "weather?",
                "tools": [
                    {
                        "type": "function",
                        "name": "get_weather",
                        "parameters": {"type": "object"},
                    }
                ],
            },
            params_enforced={
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "answer",
                        "schema": {"type": "object"},
                    },
                }
            },
        )


def test_bedrock_responses_rejects_configured_json_schema_before_translation() -> None:
    with pytest.raises(UnsupportedOperation, match="json_schema"):
        _validate_responses_request(
            Provider.BEDROCK,
            {"model": "m", "input": "answer"},
            params_enforced={
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "answer",
                        "schema": {"type": "object"},
                    },
                }
            },
            provider_model_id="anthropic.claude-3-5-sonnet-v2:0",
        )


def test_anthropic_responses_rejects_configured_stream_before_tool_routing() -> None:
    with pytest.raises(UnsupportedOperation, match="configured.*stream"):
        _validate_responses_request(
            Provider.ANTHROPIC,
            {
                "model": "m",
                "input": "weather?",
                "tools": [
                    {
                        "type": "function",
                        "name": "get_weather",
                        "parameters": {"type": "object"},
                    }
                ],
            },
            params={"stream": True},
        )


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (
            {
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "bad name", "parameters": {}},
                    }
                ],
            },
            "name",
        ),
        (
            {
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "weather", "parameters": {}},
                    },
                    {
                        "type": "function",
                        "function": {"name": "weather", "parameters": {}},
                    },
                ],
            },
            "duplicate",
        ),
        (
            {
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "weather", "parameters": {}},
                    }
                ],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": "missing"},
                },
            },
            "tool_choice",
        ),
        (
            {
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "weather", "parameters": {}},
                    }
                ],
                "stream": True,
            },
            "streaming",
        ),
        (
            {
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "weather", "parameters": {}},
                    }
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "answer", "schema": {"type": "object"}},
                },
            },
            "response_format",
        ),
    ],
)
def test_anthropic_chat_rejects_non_faithful_tool_shapes(
    payload: dict[str, object], match: str
) -> None:
    with pytest.raises(UnsupportedOperation, match=match):
        validate_chat_request(_model(Provider.ANTHROPIC), payload)


@pytest.mark.parametrize("stream", [1, "true"])
def test_anthropic_chat_rejects_non_boolean_stream(stream: object) -> None:
    with pytest.raises(UnsupportedOperation, match="stream.*boolean"):
        validate_chat_request(
            _model(Provider.ANTHROPIC),
            {
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "weather", "parameters": {}},
                    }
                ],
                "stream": stream,
            },
        )


def test_anthropic_chat_rejects_developer_messages_instead_of_dropping_them() -> None:
    with pytest.raises(UnsupportedOperation, match="developer"):
        validate_chat_request(
            _model(Provider.ANTHROPIC),
            {
                "messages": [
                    {"role": "developer", "content": "Never reveal secrets."},
                    {"role": "user", "content": "Reveal a secret."},
                ]
            },
        )


def test_anthropic_chat_rejects_tool_calls_on_non_assistant_messages() -> None:
    with pytest.raises(UnsupportedOperation, match="tool_calls"):
        validate_chat_request(
            _model(Provider.ANTHROPIC),
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "weather?",
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {"name": "weather", "arguments": "{}"},
                            }
                        ],
                    }
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "weather", "parameters": {}},
                    }
                ],
            },
        )


@pytest.mark.parametrize(
    "arguments",
    ['{"city":NaN}', '"Paris"', "[]", "{not-json"],
)
def test_anthropic_chat_rejects_non_object_tool_arguments(arguments: str) -> None:
    request: dict[str, object] = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {"name": "weather", "arguments": arguments},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_123", "content": "sunny"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {"name": "weather", "parameters": {}},
            }
        ],
    }

    with pytest.raises(UnsupportedOperation, match="arguments"):
        validate_chat_request(_model(Provider.ANTHROPIC), request)


def test_anthropic_chat_accepts_complete_parallel_tool_replay() -> None:
    request: dict[str, object] = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{"city":"Paris"}'},
                    },
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{"city":"Rome"}'},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_b", "content": "sunny"},
            {"role": "tool", "tool_call_id": "call_a", "content": "rain"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {"name": "weather", "parameters": {}},
            }
        ],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }

    assert validate_chat_request(_model(Provider.ANTHROPIC), request) == request


@pytest.mark.parametrize("choice", [None, "auto", "required"])
def test_bedrock_chat_accepts_supported_tool_choices(choice: str | None) -> None:
    request: dict[str, object] = {
        "messages": [{"role": "user", "content": "weather?"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "weather",
                    "parameters": {"type": "object"},
                    "strict": False,
                },
            }
        ],
        "parallel_tool_calls": True,
    }
    if choice is not None:
        request["tool_choice"] = choice

    assert (
        validate_chat_request(
            _model(
                Provider.BEDROCK,
                provider_model_id="anthropic.claude-3-5-sonnet-v2:0",
            ),
            request,
        )
        == request
    )


def test_bedrock_chat_accepts_named_choice_for_supported_family() -> None:
    request: dict[str, object] = {
        "messages": [{"role": "user", "content": "weather?"}],
        "tools": [
            {
                "type": "function",
                "function": {"name": "weather", "parameters": {"type": "object"}},
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "weather"}},
    }

    assert (
        validate_chat_request(
            _model(
                Provider.BEDROCK,
                provider_model_id="us.amazon.nova-pro-v1:0",
            ),
            request,
        )
        == request
    )


def test_bedrock_chat_rejects_strict_tool_schema() -> None:
    with pytest.raises(UnsupportedOperation, match="strict tool schemas"):
        validate_chat_request(
            _model(
                Provider.BEDROCK,
                provider_model_id="anthropic.claude-3-5-sonnet-v2:0",
            ),
            {
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "weather",
                            "parameters": {"type": "object"},
                            "strict": True,
                        },
                    }
                ],
            },
        )


def test_bedrock_responses_rejects_strict_tool_schema() -> None:
    with pytest.raises(UnsupportedOperation, match="strict tool schemas"):
        _validate_responses_request(
            Provider.BEDROCK,
            {
                "model": "m",
                "input": "weather?",
                "tools": [
                    {
                        "type": "function",
                        "name": "weather",
                        "parameters": {"type": "object"},
                        "strict": True,
                    }
                ],
            },
            provider_model_id="anthropic.claude-3-5-sonnet-v2:0",
        )


def test_bedrock_nova_rejects_unsupported_top_level_tool_schema_fields() -> None:
    with pytest.raises(UnsupportedOperation, match="Amazon Nova tool schemas"):
        validate_chat_request(
            _model(
                Provider.BEDROCK,
                provider_model_id="us.amazon.nova-pro-v1:0",
            ),
            {
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "weather",
                            "parameters": {
                                "type": "object",
                                "additionalProperties": False,
                            },
                        },
                    }
                ],
            },
        )


def test_bedrock_chat_rejects_empty_tool_description() -> None:
    with pytest.raises(UnsupportedOperation, match="description.*non-empty"):
        validate_chat_request(
            _model(
                Provider.BEDROCK,
                provider_model_id="anthropic.claude-3-5-sonnet-v2:0",
            ),
            {
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "weather",
                            "description": "",
                            "parameters": {},
                        },
                    }
                ],
            },
        )


@pytest.mark.parametrize(
    ("provider_model_id", "response_format", "match"),
    [
        (
            "meta.llama3-70b-instruct-v1:0",
            {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": {"type": "object"}},
            },
            "validated",
        ),
        (
            "anthropic.claude-3-5-sonnet-v2:0",
            {
                "type": "json_schema",
                "json_schema": {"name": "bad name", "schema": {"type": "object"}},
            },
            "name",
        ),
        (
            "anthropic.claude-3-5-sonnet-v2:0",
            {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": "not-an-object"},
            },
            "schema",
        ),
    ],
)
def test_bedrock_chat_validates_synthetic_structured_tool_before_routing(
    provider_model_id: str,
    response_format: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(UnsupportedOperation, match=match):
        validate_chat_request(
            _model(Provider.BEDROCK, provider_model_id=provider_model_id),
            {
                "messages": [{"role": "user", "content": "answer"}],
                "response_format": response_format,
            },
        )


@pytest.mark.parametrize(
    ("response_format", "match"),
    [
        ("json_object", "object"),
        ({"type": "unknown"}, "type"),
        ({"type": "json_object", "extra": True}, "response_format.extra"),
        (
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "answer",
                    "schema": {"type": "object"},
                    "extra": True,
                },
            },
            "json_schema.extra",
        ),
    ],
)
def test_bedrock_chat_rejects_malformed_response_format_before_routing(
    response_format: object,
    match: str,
) -> None:
    with pytest.raises(UnsupportedOperation, match=match):
        validate_chat_request(
            _model(
                Provider.BEDROCK,
                provider_model_id="anthropic.claude-3-5-sonnet-v2:0",
            ),
            {
                "messages": [{"role": "user", "content": "answer"}],
                "response_format": response_format,
            },
        )


def test_bedrock_responses_rejects_invalid_call_id_before_translation() -> None:
    with pytest.raises(UnsupportedOperation, match="call_id"):
        _validate_responses_request(
            Provider.BEDROCK,
            {
                "model": "m",
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "bad id",
                        "name": "get_weather",
                        "arguments": "{}",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "bad id",
                        "output": "sunny",
                    },
                ],
                "tools": [
                    {
                        "type": "function",
                        "name": "get_weather",
                        "parameters": {"type": "object"},
                    }
                ],
            },
            provider_model_id="anthropic.claude-3-5-sonnet-v2:0",
        )


def test_bedrock_responses_validates_synthetic_structured_tool_before_routing() -> None:
    with pytest.raises(UnsupportedOperation, match="validated"):
        _validate_responses_request(
            Provider.BEDROCK,
            {
                "model": "m",
                "input": "answer",
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "answer",
                        "schema": {"type": "object"},
                    }
                },
            },
            provider_model_id="meta.llama3-70b-instruct-v1:0",
        )


@pytest.mark.parametrize(
    ("provider_model_id", "extra", "match"),
    [
        (
            "anthropic.claude-3-5-sonnet-v2:0",
            {"tool_choice": "none"},
            "tool_choice='none'",
        ),
        (
            "anthropic.claude-3-5-sonnet-v2:0",
            {"parallel_tool_calls": False},
            "parallel_tool_calls=false",
        ),
        (
            "meta.llama3-70b-instruct-v1:0",
            {},
            "validated",
        ),
        (
            "arn:aws:bedrock:eu-west-1:123456789012:inference-profile/claude",
            {},
            "validated",
        ),
        (
            "proxy.anthropic.claude-3-5-sonnet-v2:0",
            {},
            "validated",
        ),
    ],
)
def test_bedrock_chat_rejects_unsupported_tool_capabilities(
    provider_model_id: str,
    extra: dict[str, object],
    match: str,
) -> None:
    request: dict[str, object] = {
        "messages": [{"role": "user", "content": "weather?"}],
        "tools": [
            {
                "type": "function",
                "function": {"name": "weather", "parameters": {}},
            }
        ],
        **extra,
    }

    with pytest.raises(UnsupportedOperation, match=match):
        validate_chat_request(
            _model(Provider.BEDROCK, provider_model_id=provider_model_id),
            request,
        )


def test_anthropic_chat_tool_count_limit_accepts_boundary_and_rejects_next() -> None:
    def request(tool_count: int) -> dict[str, object]:
        return {
            "messages": [{"role": "user", "content": "pick a tool"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": f"tool_{index}", "parameters": {}},
                }
                for index in range(tool_count)
            ],
        }

    assert validate_chat_request(_model(Provider.ANTHROPIC), request(MAX_TOOL_COUNT)) == request(
        MAX_TOOL_COUNT
    )
    with pytest.raises(UnsupportedOperation, match="at most"):
        validate_chat_request(_model(Provider.ANTHROPIC), request(MAX_TOOL_COUNT + 1))


def test_anthropic_chat_tool_schema_depth_limit_accepts_boundary_and_rejects_next() -> None:
    def nested(depth: int) -> dict[str, object]:
        value: dict[str, object] = {}
        for _ in range(depth - 1):
            value = {"next": value}
        return value

    def request(depth: int) -> dict[str, object]:
        return {
            "messages": [{"role": "user", "content": "use it"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "deep_tool", "parameters": nested(depth)},
                }
            ],
        }

    assert validate_chat_request(
        _model(Provider.ANTHROPIC), request(MAX_TOOL_JSON_DEPTH)
    ) == request(MAX_TOOL_JSON_DEPTH)
    with pytest.raises(UnsupportedOperation, match="deeper"):
        validate_chat_request(_model(Provider.ANTHROPIC), request(MAX_TOOL_JSON_DEPTH + 1))


@pytest.mark.parametrize(
    "payload,match",
    [
        ({"tools": [{"type": "web_search"}]}, r"tools\[0\]\.type"),
        ({"tools": [{"type": "custom", "name": "shell"}]}, r"tools\[0\]\.type"),
        (
            {
                "tools": [
                    {
                        "type": "function",
                        "name": "weather",
                        "parameters": {},
                        "allowed_callers": ["direct"],
                    }
                ]
            },
            r"tools\[0\]\.allowed_callers",
        ),
        (
            {
                "tools": [
                    {
                        "type": "function",
                        "name": "weather",
                        "parameters": {},
                        "defer_loading": True,
                    }
                ]
            },
            r"tools\[0\]\.defer_loading",
        ),
        (
            {
                "tools": [
                    {
                        "type": "function",
                        "name": "weather",
                        "parameters": {},
                        "output_schema": {"type": "string"},
                    }
                ]
            },
            r"tools\[0\]\.output_schema",
        ),
        ({"tools": [], "tool_choice": {"type": "custom", "name": "shell"}}, "tool_choice"),
        ({"tools": [], "parallel_tool_calls": "yes"}, "parallel_tool_calls"),
        (
            {
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call_123",
                        "name": "weather",
                        "arguments": {},
                    }
                ]
            },
            "arguments",
        ),
        (
            {
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call_123",
                        "name": "weather",
                        "arguments": "{}",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_123",
                        "output": [{"type": "input_text", "text": "sunny"}],
                    },
                ]
            },
            "output",
        ),
        (
            {
                "input": [
                    {
                        "type": "function_call_output",
                        "call_id": "call_missing",
                        "output": "sunny",
                    }
                ]
            },
            "matching function_call",
        ),
        (
            {
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call_123",
                        "name": "weather",
                        "arguments": "{}",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_123",
                        "name": "weather",
                        "arguments": "{}",
                    },
                ]
            },
            "duplicate call_id",
        ),
    ],
)
def test_databricks_responses_reject_lossy_or_malformed_tool_shapes(
    payload: dict[str, object], match: str
) -> None:
    with pytest.raises(UnsupportedOperation, match=match):
        _validate_responses_request(
            Provider.DATABRICKS,
            {"model": "m", "input": "hi", **payload},
        )


def test_databricks_responses_reject_streaming_tools_until_phase_2() -> None:
    with pytest.raises(UnsupportedOperation, match="streaming tool"):
        _validate_responses_request(
            Provider.DATABRICKS,
            {
                "model": "m",
                "input": "weather?",
                "stream": True,
                "tools": [
                    {
                        "type": "function",
                        "name": "get_weather",
                        "parameters": {"type": "object"},
                    }
                ],
            },
        )


@pytest.mark.parametrize("stream", [1, "true"])
def test_responses_reject_non_boolean_stream_before_dispatch(stream: object) -> None:
    with pytest.raises(UnsupportedOperation, match="stream.*boolean"):
        _validate_responses_request(
            Provider.DATABRICKS,
            {
                "model": "m",
                "input": "weather?",
                "stream": stream,
                "tools": [
                    {
                        "type": "function",
                        "name": "get_weather",
                        "parameters": {"type": "object"},
                    }
                ],
            },
        )


@pytest.mark.parametrize("status", ["in_progress", "incomplete"])
def test_databricks_responses_reject_incomplete_replayed_tool_items(status: str) -> None:
    with pytest.raises(UnsupportedOperation, match="status"):
        _validate_responses_request(
            Provider.DATABRICKS,
            {
                "model": "m",
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call_123",
                        "name": "weather",
                        "arguments": "{}",
                        "status": status,
                    }
                ],
            },
        )


@pytest.mark.parametrize(
    "input_items,match",
    [
        (
            [
                {
                    "type": "function_call",
                    "call_id": "call_123",
                    "name": "weather",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_123",
                    "output": "sunny",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_123",
                    "output": "sunny again",
                },
            ],
            "duplicate function_call_output",
        ),
        (
            [
                {
                    "type": "function_call",
                    "call_id": "call_123",
                    "name": "weather",
                    "arguments": "{}",
                },
                {"role": "user", "content": "continue"},
                {
                    "type": "function_call_output",
                    "call_id": "call_123",
                    "output": "sunny",
                },
            ],
            "unresolved function_call",
        ),
        (
            [
                {
                    "type": "function_call",
                    "call_id": "call_123",
                    "name": "weather",
                    "arguments": "{}",
                },
                {
                    "type": "function_call",
                    "call_id": "call_456",
                    "name": "weather",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_123",
                    "output": "sunny",
                },
                {
                    "type": "function_call",
                    "call_id": "call_789",
                    "name": "weather",
                    "arguments": "{}",
                },
            ],
            "unresolved function_call",
        ),
        (
            [
                {
                    "type": "function_call",
                    "call_id": "call_123",
                    "name": "weather",
                    "arguments": "{}",
                }
            ],
            "unresolved function_call",
        ),
        (
            [
                {
                    "type": "function_call",
                    "call_id": "call_123",
                    "name": "weather",
                    "arguments": "{}",
                },
                {
                    "type": "function_call",
                    "call_id": "call_456",
                    "name": "weather",
                    "arguments": "{}",
                },
            ],
            "unresolved function_call",
        ),
    ],
)
def test_databricks_responses_reject_ambiguous_tool_replay_order(
    input_items: list[dict[str, object]], match: str
) -> None:
    with pytest.raises(UnsupportedOperation, match=match):
        _validate_responses_request(
            Provider.DATABRICKS,
            {"model": "m", "input": input_items},
        )


def test_databricks_responses_validates_large_parallel_replay() -> None:
    calls = [
        {
            "type": "function_call",
            "call_id": f"call_{index}",
            "name": "lookup",
            "arguments": "{}",
        }
        for index in range(2_000)
    ]
    outputs = [
        {
            "type": "function_call_output",
            "call_id": f"call_{index}",
            "output": str(index),
        }
        for index in range(2_000)
    ]
    request: dict[str, object] = {"model": "m", "input": [*calls, *outputs]}

    assert _validate_responses_request(Provider.DATABRICKS, request) == request


def test_chat_only_responses_accept_text_and_structured_output_subset() -> None:
    _validate_responses_request(
        Provider.ANTHROPIC,
        {
            "model": "m",
            "instructions": "Be concise",
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                }
            ],
            "max_output_tokens": 200,
            "temperature": 0.2,
            "top_p": 0.9,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "answer",
                    "schema": {"type": "object"},
                    "strict": True,
                }
            },
            "store": False,
            "stream": False,
        },
    )


def test_chat_only_responses_reject_multimodal_input() -> None:
    with pytest.raises(UnsupportedOperation, match="input_image"):
        _validate_responses_request(
            Provider.VERTEX_AI,
            {
                "model": "m",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "describe"},
                            {"type": "input_image", "image_url": "https://example.com/cat.png"},
                        ],
                    }
                ],
            },
        )


def test_vertex_responses_reject_function_call_output_input() -> None:
    with pytest.raises(UnsupportedOperation, match="function_call_output"):
        _validate_responses_request(
            Provider.VERTEX_AI,
            {
                "model": "m",
                "input": [
                    {
                        "type": "function_call_output",
                        "call_id": "call_123",
                        "output": "sunny",
                    }
                ],
            },
        )


def test_chat_only_responses_reject_unsupported_text_options() -> None:
    with pytest.raises(UnsupportedOperation, match=r"text\.verbosity"):
        _validate_responses_request(
            Provider.ANTHROPIC,
            {
                "model": "m",
                "input": "hi",
                "text": {"format": {"type": "text"}, "verbosity": "low"},
            },
        )


@pytest.mark.parametrize(
    "payload,match",
    [
        (
            {"input": [{"role": "developer", "content": "Never disclose secrets"}]},
            "developer",
        ),
        (
            {"input": [{"role": "user", "content": "hi", "phase": "commentary"}]},
            r"input\.phase",
        ),
        (
            {
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "hi",
                                "prompt_cache_breakpoint": {"type": "ephemeral"},
                            }
                        ],
                    }
                ]
            },
            r"input\.content\.prompt_cache_breakpoint",
        ),
        (
            {
                "input": "hi",
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "answer",
                        "schema": {"type": "object"},
                        "description": "The answer payload",
                    }
                },
            },
            r"text\.format\.description",
        ),
    ],
)
def test_chat_only_responses_reject_nested_fields_that_translation_would_drop(
    payload: dict[str, object], match: str
) -> None:
    with pytest.raises(UnsupportedOperation, match=match):
        _validate_responses_request(Provider.ANTHROPIC, {"model": "m", **payload})


@pytest.mark.parametrize(
    "payload,match",
    [
        ({"input": "hi", "text": {"format": {"type": []}}}, "text format"),
        ({"input": [{"role": [], "content": "hi"}]}, "input role"),
        (
            {"input": [{"role": "user", "content": [{"type": [], "text": "hi"}]}]},
            "input content type",
        ),
    ],
)
def test_chat_only_responses_rejects_non_string_discriminators(
    payload: dict[str, object], match: str
) -> None:
    with pytest.raises(UnsupportedOperation, match=match):
        _validate_responses_request(Provider.ANTHROPIC, {"model": "m", **payload})


@pytest.mark.parametrize("instructions", [123, {"text": "hi"}, ["hi"]])
def test_chat_only_responses_rejects_non_string_instructions(instructions: object) -> None:
    with pytest.raises(UnsupportedOperation, match="instructions"):
        _validate_responses_request(
            Provider.ANTHROPIC,
            {"model": "m", "input": "hi", "instructions": instructions},
        )


@pytest.mark.parametrize("configured_in", ["params", "params_enforced"])
def test_chat_only_responses_reject_tool_configured_on_model(configured_in: str) -> None:
    config: dict[str, object] = {"tools": [{"type": "function", "function": {"name": "weather"}}]}
    with pytest.raises(UnsupportedOperation, match=r"configured model field\(s\): tools"):
        if configured_in == "params":
            _validate_responses_request(
                Provider.DATABRICKS,
                {"model": "m", "input": "hi"},
                params=config,
            )
        else:
            _validate_responses_request(
                Provider.DATABRICKS,
                {"model": "m", "input": "hi"},
                params_enforced=config,
            )


@pytest.mark.parametrize("provider", [Provider.OPENAI, Provider.AZURE_OPENAI])
def test_native_responses_bypass_emulation_capability_gate(provider: Provider) -> None:
    _validate_responses_request(
        provider,
        {
            "model": "m",
            "input": [{"type": "input_image", "image_url": "https://example.com/cat.png"}],
            "tools": [{"type": "function", "name": "weather", "parameters": {}}],
            "include": ["reasoning.encrypted_content"],
        },
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("background", True),
        ("context_management", [{"type": "compaction", "compact_threshold": 1000}]),
        ("conversation", "conv_123"),
        ("previous_response_id", "resp_123"),
        ("prompt", {"id": "pmpt_123"}),
        ("prompt_cache_retention", "24h"),
        ("service_tier", "flex"),
        ("store", True),
    ],
)
@pytest.mark.parametrize("provider", [Provider.OPENAI, Provider.AZURE_OPENAI])
def test_native_responses_reject_unmetered_or_cross_tenant_state(
    provider: Provider, field: str, value: object
) -> None:
    with pytest.raises(UnsupportedOperation, match=field):
        _validate_responses_request(
            provider,
            {"model": "m", "input": "hi", field: value},
        )


@pytest.mark.parametrize(
    "tools,match",
    [
        ([{"type": "web_search"}], r"tools\[0\]\.web_search"),
        (
            [{"type": "file_search", "vector_store_ids": ["vs_other_tenant"]}],
            r"tools\[0\]\.file_search",
        ),
        (
            [{"type": "code_interpreter", "container": {"type": "auto"}}],
            r"tools\[0\]\.code_interpreter",
        ),
    ],
)
def test_native_responses_reject_hosted_tools_with_unmetered_fees(
    tools: list[dict[str, object]], match: str
) -> None:
    with pytest.raises(UnsupportedOperation, match=match):
        _validate_responses_request(
            Provider.OPENAI,
            {"model": "m", "input": "hi", "tools": tools},
        )


def test_native_responses_reject_nested_provider_resource_ids() -> None:
    with pytest.raises(UnsupportedOperation, match=r"input\[0\]\.content\[0\]\.file_id"):
        _validate_responses_request(
            Provider.OPENAI,
            {
                "model": "m",
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_file", "file_id": "file_other_tenant"}],
                    }
                ],
            },
        )


def test_native_responses_reject_implicit_item_reference() -> None:
    with pytest.raises(UnsupportedOperation, match=r"input\[0\]\.id"):
        _validate_responses_request(
            Provider.OPENAI,
            {"model": "m", "input": [{"id": "item_other_tenant"}]},
        )


def test_native_responses_rejects_non_string_nested_discriminator() -> None:
    with pytest.raises(UnsupportedOperation, match=r"input\[0\]\.content\[0\]\.type"):
        _validate_responses_request(
            Provider.OPENAI,
            {
                "model": "m",
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": [], "file_id": "file_other_tenant"}],
                    }
                ],
            },
        )


def test_native_responses_allows_function_tools_and_forces_stateless_mode() -> None:
    governed = _validate_responses_request(
        Provider.OPENAI,
        {
            "model": "m",
            "input": "hi",
            "tools": [
                {
                    "type": "function",
                    "name": "weather",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_id": {"type": "string"},
                            "container": {"type": "string"},
                        },
                    },
                }
            ],
        },
    )

    assert governed["store"] is False


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
