"""Vertex tool-response validation and synthetic call-ID replay tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from litestar_gateway.domain.chat_tool_policy import MAX_VERTEX_THOUGHT_SIGNATURE_BYTES
from litestar_gateway.domain.entities import Model, ModelType, Provider
from litestar_gateway.domain.exceptions import UpstreamResponseInvalid
from litestar_gateway.infrastructure.llm.vertex_adapter import (
    from_gemini_response,
    to_gemini_request,
)

SIGNATURE = b"\x00vertex\xffsignature"

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
    "part",
    [
        {
            "function_call": {"id": "call_a", "name": "unknown", "args": {}},
            "thought_signature": SIGNATURE,
        },
        {
            "function_call": {"id": "call_a", "name": "weather", "args": []},
            "thought_signature": SIGNATURE,
        },
        {
            "function_call": {"id": "call_a", "name": "weather", "args": {}},
            "thought_signature": "not-bytes",
        },
    ],
)
def test_response_rejects_malformed_tool_parts_with_billable_usage(
    part: dict[str, object],
) -> None:
    response = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [part]},
                "finish_reason": "STOP",
            }
        ],
        "usage_metadata": {
            "prompt_token_count": 4,
            "candidates_token_count": 2,
            "total_token_count": 6,
        },
    }

    with pytest.raises(UpstreamResponseInvalid) as raised:
        from_gemini_response(response, expected_tool_names={"weather"})

    assert raised.value.billable_response == {
        "usage": {
            "prompt_tokens": 4,
            "completion_tokens": 2,
            "total_tokens": 6,
        }
    }


def test_response_generates_unique_ids_when_vertex_omits_them() -> None:
    translated = from_gemini_response(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "function_call": {"name": "weather", "args": {}},
                                "thought_signature": SIGNATURE,
                            },
                            {"function_call": {"name": "weather", "args": {}}},
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
            "response_id": "response-a",
        },
        expected_tool_names={"weather"},
    )

    calls = translated["choices"][0]["message"]["tool_calls"]
    assert calls[0]["id"].startswith("function-call-gateway-")
    assert calls[1]["id"].startswith("function-call-gateway-")
    assert calls[0]["id"] != calls[1]["id"]
    assert calls[0]["extra_content"]["litestar_gateway"] == {"synthetic_call_id": True}
    assert calls[1]["extra_content"] == {"litestar_gateway": {"synthetic_call_id": True}}

    replay = to_gemini_request(
        {
            "messages": [
                {"role": "assistant", "tool_calls": calls},
                {"role": "tool", "tool_call_id": calls[0]["id"], "content": "{}"},
                {"role": "tool", "tool_call_id": calls[1]["id"], "content": "{}"},
            ],
            "tools": TOOLS,
        },
        _model(),
    )
    assert replay["contents"][0]["parts"] == [
        {
            "function_call": {"name": "weather", "args": {}},
            "thought_signature": SIGNATURE,
        },
        {"function_call": {"name": "weather", "args": {}}},
    ]
    assert replay["contents"][1]["parts"] == [
        {
            "function_response": {
                "name": "weather",
                "response": {},
            }
        },
        {
            "function_response": {
                "name": "weather",
                "response": {},
            }
        },
    ]


def test_response_preserves_a_provider_id_that_uses_the_gateway_prefix() -> None:
    provider_call_id = "function-call-gateway-provider-native"
    translated = from_gemini_response(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "function_call": {
                                    "id": provider_call_id,
                                    "name": "weather",
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
        },
        expected_tool_names={"weather"},
    )
    call = translated["choices"][0]["message"]["tool_calls"][0]
    assert "litestar_gateway" not in call["extra_content"]

    replay = to_gemini_request(
        {
            "messages": [
                {"role": "assistant", "tool_calls": [call]},
                {"role": "tool", "tool_call_id": provider_call_id, "content": "{}"},
            ],
            "tools": TOOLS,
        },
        _model(),
    )

    assert replay["contents"][0]["parts"][0]["function_call"]["id"] == provider_call_id
    assert replay["contents"][1]["parts"][0]["function_response"]["id"] == provider_call_id


def test_response_accepts_documented_text_thought_signature() -> None:
    translated = from_gemini_response(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "text": "done",
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
    )

    assert translated["choices"][0]["message"]["content"] == "done"


@pytest.mark.parametrize("call_id", ["call_a", None])
def test_response_requires_gemini_3_signature_when_capability_demands_it(
    call_id: str | None,
) -> None:
    function_call: dict[str, object] = {
        "name": "weather",
        "args": {},
    }
    if call_id is not None:
        function_call["id"] = call_id
    response = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "function_call": function_call,
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

    with pytest.raises(UpstreamResponseInvalid, match="omitted.*signature"):
        from_gemini_response(
            response,
            expected_tool_names={"weather"},
            require_thought_signature=True,
        )


def test_response_rejects_tool_call_when_choice_is_none() -> None:
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

    with pytest.raises(UpstreamResponseInvalid, match="tool_choice='none'"):
        from_gemini_response(
            response,
            expected_tool_names={"weather"},
            forbid_tool_call=True,
        )


@pytest.mark.parametrize("candidates", [{"unexpected": True}, 1, True])
def test_response_rejects_malformed_candidates_with_billable_usage(
    candidates: object,
) -> None:
    response = {
        "candidates": candidates,
        "usage_metadata": {
            "prompt_token_count": 4,
            "candidates_token_count": 2,
            "total_token_count": 6,
        },
    }

    with pytest.raises(UpstreamResponseInvalid) as raised:
        from_gemini_response(response)

    assert raised.value.billable_response["usage"]["total_tokens"] == 6


def test_response_rejects_oversized_signature_with_billable_usage() -> None:
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
                                "args": {},
                            },
                            "thought_signature": b"x" * (MAX_VERTEX_THOUGHT_SIGNATURE_BYTES + 1),
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

    with pytest.raises(UpstreamResponseInvalid) as raised:
        from_gemini_response(response, expected_tool_names={"weather"})

    assert raised.value.billable_response["usage"]["total_tokens"] == 6
