from __future__ import annotations

import base64
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from openai.types.chat import ChatCompletion

from litestar_gateway.domain.chat_tool_policy import (
    MAX_TOOL_JSON_DEPTH,
    validate_chat_request,
)
from litestar_gateway.domain.entities import Model, ModelType, Provider
from litestar_gateway.domain.exceptions import UnsupportedOperation, UpstreamResponseInvalid
from litestar_gateway.infrastructure.llm.vertex_adapter import (
    _tool_response_contract,
    from_gemini_response,
    to_gemini_request,
)

SIGNATURE = b"\x00vertex\xffsignature"
SIGNATURE_B64 = base64.b64encode(SIGNATURE).decode("ascii")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "weather",
            "description": "Get weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


def _model() -> Model:
    return Model(
        id=uuid4(),
        team_id=uuid4(),
        name="vertex",
        provider=Provider.VERTEX_AI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gemini-3-pro",
        params={},
        params_enforced={},
        api_version=None,
        input_cost_per_token=None,
        output_cost_per_token=None,
        enabled=True,
        created_at=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ("auto", {"mode": "AUTO"}),
        ("none", {"mode": "NONE"}),
        ("required", {"mode": "ANY"}),
        (
            {"type": "function", "function": {"name": "weather"}},
            {"mode": "ANY", "allowed_function_names": ["weather"]},
        ),
    ],
)
def test_request_maps_tools_and_choices(choice: object, expected: dict[str, object]) -> None:
    translated = to_gemini_request(
        {
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": TOOLS,
            "tool_choice": choice,
            "parallel_tool_calls": True,
        },
        _model(),
    )

    assert translated["config"]["tools"] == [
        {
            "function_declarations": [
                {
                    "name": "weather",
                    "description": "Get weather for a city.",
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                }
            ]
        }
    ]
    assert translated["config"]["tool_config"] == {"function_calling_config": expected}


def test_named_tool_choice_rejects_a_different_upstream_tool() -> None:
    calendar_tool = {
        "type": "function",
        "function": {
            "name": "calendar",
            "parameters": {"type": "object"},
        },
    }
    request = {
        "messages": [{"role": "user", "content": "weather?"}],
        "tools": [*TOOLS, calendar_tool],
        "tool_choice": {"type": "function", "function": {"name": "weather"}},
    }
    names, require_call, forbid_call, require_signature = _tool_response_contract(request, _model())
    response = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "function_call": {
                                "id": "call_a",
                                "name": "calendar",
                                "args": {},
                            },
                            "thought_signature": SIGNATURE,
                        }
                    ],
                },
                "finish_reason": "STOP",
            }
        ],
        "usage_metadata": {
            "prompt_token_count": 4,
            "candidates_token_count": 2,
            "total_token_count": 6,
        },
    }

    with pytest.raises(UpstreamResponseInvalid, match="malformed function call"):
        from_gemini_response(
            response,
            expected_tool_names=names,
            require_tool_call=require_call,
            forbid_tool_call=forbid_call,
            require_thought_signature=require_signature,
        )


def test_request_replays_parallel_calls_and_results_with_exact_signature() -> None:
    request = {
        "messages": [
            {"role": "user", "content": "Paris and Rome?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {
                            "name": "weather",
                            "arguments": '{"city":"Paris"}',
                        },
                        "extra_content": {"google": {"thought_signature": SIGNATURE_B64}},
                    },
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {
                            "name": "weather",
                            "arguments": '{"city":"Rome"}',
                        },
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_a",
                "content": '{"temperature":18}',
            },
            {
                "role": "tool",
                "tool_call_id": "call_b",
                "content": "sunny",
            },
        ],
        "tools": TOOLS,
    }

    translated = to_gemini_request(request, _model())

    assert translated["contents"] == [
        {"role": "user", "parts": [{"text": "Paris and Rome?"}]},
        {
            "role": "model",
            "parts": [
                {
                    "function_call": {
                        "id": "call_a",
                        "name": "weather",
                        "args": {"city": "Paris"},
                    },
                    "thought_signature": SIGNATURE,
                },
                {
                    "function_call": {
                        "id": "call_b",
                        "name": "weather",
                        "args": {"city": "Rome"},
                    }
                },
            ],
        },
        {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "id": "call_a",
                        "name": "weather",
                        "response": {"temperature": 18},
                    }
                },
                {
                    "function_response": {
                        "id": "call_b",
                        "name": "weather",
                        "response": {"output": "sunny"},
                    }
                },
            ],
        },
    ]


def test_request_preserves_each_signature_across_sequential_steps() -> None:
    second_signature = b"\x01second\xfe"
    request = {
        "messages": [
            {"role": "user", "content": "Compare both cities."},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {
                            "name": "weather",
                            "arguments": '{"city":"Paris"}',
                        },
                        "extra_content": {"google": {"thought_signature": SIGNATURE_B64}},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_a", "content": "{}"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {
                            "name": "weather",
                            "arguments": '{"city":"Rome"}',
                        },
                        "extra_content": {
                            "google": {
                                "thought_signature": base64.b64encode(second_signature).decode(
                                    "ascii"
                                )
                            }
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_b", "content": "{}"},
        ],
        "tools": TOOLS,
    }

    translated = to_gemini_request(request, _model())

    assert translated["contents"][1]["parts"][0]["thought_signature"] == SIGNATURE
    assert translated["contents"][3]["parts"][0]["thought_signature"] == second_signature


def test_response_maps_parallel_calls_and_signature_carrier() -> None:
    response = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "function_call": {
                                "id": "call_a",
                                "name": "weather",
                                "args": {"city": "Paris"},
                            },
                            "thought_signature": SIGNATURE,
                        },
                        {
                            "function_call": {
                                "id": "call_b",
                                "name": "weather",
                                "args": {"city": "Rome"},
                            }
                        },
                    ],
                },
                "finish_reason": "STOP",
            }
        ],
        "usage_metadata": {
            "prompt_token_count": 4,
            "candidates_token_count": 2,
            "total_token_count": 6,
        },
        "model_version": "gemini-3-pro",
        "response_id": "response-a",
    }

    translated = from_gemini_response(response, expected_tool_names={"weather"})
    choice = translated["choices"][0]

    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"] == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_a",
                "type": "function",
                "function": {
                    "name": "weather",
                    "arguments": '{"city":"Paris"}',
                },
                "extra_content": {"google": {"thought_signature": SIGNATURE_B64}},
            },
            {
                "id": "call_b",
                "type": "function",
                "function": {
                    "name": "weather",
                    "arguments": '{"city":"Rome"}',
                },
            },
        ],
    }


def test_stock_openai_sdk_preserves_signature_for_replay() -> None:
    translated = from_gemini_response(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "function_call": {
                                    "name": "weather",
                                    "args": {"city": "Paris"},
                                },
                                "thought_signature": SIGNATURE,
                            }
                        ],
                    },
                    "finish_reason": "STOP",
                }
            ],
            "usage_metadata": {
                "prompt_token_count": 4,
                "candidates_token_count": 2,
                "total_token_count": 6,
            },
            "model_version": "gemini-3-pro",
            "response_id": "response-a",
        },
        expected_tool_names={"weather"},
    )

    sdk_response = ChatCompletion.model_validate(translated)
    assistant = sdk_response.choices[0].message.model_dump()
    call_id = assistant["tool_calls"][0]["id"]
    assert assistant["tool_calls"][0]["extra_content"]["litestar_gateway"] == {
        "synthetic_call_id": True
    }
    replay = to_gemini_request(
        {
            "messages": [
                {"role": "user", "content": "weather?"},
                assistant,
                {"role": "tool", "tool_call_id": call_id, "content": "{}"},
            ],
            "tools": TOOLS,
        },
        _model(),
    )

    assert replay["contents"][1]["parts"][0]["thought_signature"] == SIGNATURE
    assert "id" not in replay["contents"][1]["parts"][0]["function_call"]


def test_gemini_3_replay_requires_signature_but_gemini_2_5_does_not() -> None:
    request = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "weather", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_a", "content": "{}"},
        ],
        "tools": TOOLS,
    }

    with pytest.raises(UnsupportedOperation, match="requires thought_signature"):
        validate_chat_request(_model(), request)

    legacy = _model()
    object.__setattr__(legacy, "provider_model_id", "gemini-2.5-flash")
    assert validate_chat_request(legacy, request) == request


@pytest.mark.parametrize(
    "model_id",
    [
        "text-bison",
        "gemini-2.0-flash",
        "gemini-3-pro-image-preview",
        "gemini-embedding-001",
        "gemini-3.99-pro-experimental",
        "gemini-3-pro-foo",
        "gemini-2.5-flash-xyz",
    ],
)
def test_request_rejects_unvalidated_vertex_tool_models(model_id: str) -> None:
    model = _model()
    object.__setattr__(model, "provider_model_id", model_id)

    with pytest.raises(UnsupportedOperation, match="validated Gemini"):
        validate_chat_request(
            model,
            {
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": TOOLS,
            },
        )


@pytest.mark.parametrize(
    "parameters",
    [
        {"type": "string"},
        {"type": "object", "additionalProperties": False},
        {"type": "object", "properties": {"city": {"oneOf": [{"type": "string"}]}}},
        {
            "type": "object",
            "nullable": "false",
            "properties": {"city": {"type": 7, "enum": "oops"}},
        },
        {"type": "object", "properties": {"city": {"type": []}}},
        {"type": "object", "properties": {"city": {"type": {}}}},
    ],
)
def test_request_rejects_untranslated_vertex_schema_keywords(
    parameters: dict[str, object],
) -> None:
    tools = [
        {
            "type": "function",
            "function": {"name": "weather", "parameters": parameters},
        }
    ]

    with pytest.raises(UnsupportedOperation, match="parameters"):
        validate_chat_request(
            _model(),
            {
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": tools,
            },
        )


@pytest.mark.parametrize("name", ["1weather", "-weather"])
def test_request_rejects_invalid_vertex_function_names(name: str) -> None:
    tools = [
        {
            "type": "function",
            "function": {"name": name, "parameters": {"type": "object"}},
        }
    ]

    with pytest.raises(UnsupportedOperation, match="function.name"):
        validate_chat_request(
            _model(),
            {"messages": [{"role": "user", "content": "weather?"}], "tools": tools},
        )


@pytest.mark.parametrize("property_name", ["", "bad name", "x" * 65])
def test_request_rejects_invalid_vertex_parameter_names(property_name: str) -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "weather",
                "parameters": {
                    "type": "object",
                    "properties": {property_name: {"type": "string"}},
                },
            },
        }
    ]

    with pytest.raises(UnsupportedOperation, match="properties"):
        validate_chat_request(
            _model(),
            {"messages": [{"role": "user", "content": "weather?"}], "tools": tools},
        )


def test_request_rejects_invalid_vertex_required_parameter_name() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "weather",
                "parameters": {
                    "type": "object",
                    "required": ["bad name"],
                },
            },
        }
    ]

    with pytest.raises(UnsupportedOperation, match="required"):
        validate_chat_request(
            _model(),
            {"messages": [{"role": "user", "content": "weather?"}], "tools": tools},
        )


def test_request_accepts_a_128_character_vertex_function_name() -> None:
    name = "_" + ("a" * 127)
    translated = to_gemini_request(
        {
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": name, "parameters": {"type": "object"}},
                }
            ],
        },
        _model(),
    )

    assert translated["config"]["tools"][0]["function_declarations"][0]["name"] == name


def test_request_rejects_deep_vertex_schema_without_recursion_error() -> None:
    schema: dict[str, object] = {"type": "string"}
    for _ in range(MAX_TOOL_JSON_DEPTH + 1_100):
        schema = {"type": "array", "items": schema}
    tools = [
        {
            "type": "function",
            "function": {
                "name": "weather",
                "parameters": {
                    "type": "object",
                    "properties": {"nested": schema},
                },
            },
        }
    ]

    with pytest.raises(UnsupportedOperation, match="deeper than"):
        validate_chat_request(
            _model(),
            {
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": tools,
            },
        )


@pytest.mark.parametrize(
    "extra_content",
    [
        {},
        {"google": {}},
        {"google": {"thought_signature": ""}},
        {"google": {"thought_signature": "not base64!"}},
        {"google": {"thought_signature": "skip_thought_signature_validator"}},
        {
            "google": {
                "thought_signature": base64.b64encode(b"skip_thought_signature_validator").decode(
                    "ascii"
                )
            }
        },
        {"google": {"thought_signature": SIGNATURE_B64, "unknown": True}},
        {"google": {"thought_signature": SIGNATURE_B64}, "unknown": True},
    ],
)
def test_request_rejects_malformed_signature_carrier(extra_content: object) -> None:
    request = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "weather", "arguments": "{}"},
                        "extra_content": extra_content,
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_a", "content": "{}"},
        ],
        "tools": TOOLS,
    }

    with pytest.raises(UnsupportedOperation, match="extra_content|thought_signature"):
        validate_chat_request(_model(), request)


@pytest.mark.parametrize(
    "extra_content",
    [
        {"google": None},
        {"litestar_gateway": None},
        {"litestar_gateway": {"synthetic_call_id": 1}},
    ],
)
def test_request_rejects_malformed_gateway_call_id_metadata(
    extra_content: object,
) -> None:
    call_id = "function-call-gateway-test"
    request = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "weather", "arguments": "{}"},
                        "extra_content": extra_content,
                    }
                ],
            },
            {"role": "tool", "tool_call_id": call_id, "content": "{}"},
        ],
        "tools": TOOLS,
    }

    with pytest.raises(UnsupportedOperation, match="extra_content"):
        validate_chat_request(_model(), request)


def test_request_rejects_signature_moved_to_second_parallel_call() -> None:
    request = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "weather", "arguments": "{}"},
                    },
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "weather", "arguments": "{}"},
                        "extra_content": {"google": {"thought_signature": SIGNATURE_B64}},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_a", "content": "{}"},
            {"role": "tool", "tool_call_id": "call_b", "content": "{}"},
        ],
        "tools": TOOLS,
    }

    with pytest.raises(UnsupportedOperation, match="thought_signature"):
        validate_chat_request(_model(), request)


def test_request_rejects_parallel_results_in_a_different_order() -> None:
    request = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "weather", "arguments": "{}"},
                        "extra_content": {"google": {"thought_signature": SIGNATURE_B64}},
                    },
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "weather", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_b", "content": "{}"},
            {"role": "tool", "tool_call_id": "call_a", "content": "{}"},
        ],
        "tools": TOOLS,
    }

    with pytest.raises(UnsupportedOperation, match="tool result"):
        validate_chat_request(_model(), request)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("stream", True, "streaming tool calls"),
        ("parallel_tool_calls", False, "parallel_tool_calls=false"),
    ],
)
def test_request_rejects_unsupported_tool_controls(field: str, value: object, match: str) -> None:
    request = {
        "messages": [{"role": "user", "content": "weather?"}],
        "tools": TOOLS,
        field: value,
    }

    with pytest.raises(UnsupportedOperation, match=match):
        validate_chat_request(_model(), request)


@pytest.mark.parametrize("strict", [False, True])
def test_request_rejects_strict_tool_schema(strict: bool) -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "weather",
                "parameters": {"type": "object"},
                "strict": strict,
            },
        }
    ]

    with pytest.raises(UnsupportedOperation, match="strict"):
        validate_chat_request(
            _model(),
            {
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": tools,
            },
        )
