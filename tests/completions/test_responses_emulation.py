"""Pure Responses ↔ Chat translation contracts for chat-only providers."""

from __future__ import annotations

import pytest

from litestar_gateway.domain.exceptions import UpstreamResponseInvalid, UpstreamUnavailable
from litestar_gateway.infrastructure.llm.responses_emulation import (
    to_chat_completions,
    to_responses,
)

WEATHER_TOOL = {
    "type": "function",
    "name": "get_weather",
    "description": "Get weather for a city.",
    "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    },
    "strict": True,
}


def _function_call(
    call_id: str = "call_abc123",
    *,
    city: str = "Paris",
) -> dict[str, object]:
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "status": "completed",
        "call_id": call_id,
        "name": "get_weather",
        "arguments": f'{{"city": "{city}"}}',
    }


def _chat_tool_response(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "id": "chatcmpl-tool",
        "object": "chat.completion",
        "created": 123,
        "model": "my-endpoint",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls
                    if tool_calls is not None
                    else [
                        {
                            "id": "call_abc123",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city": "Paris"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 9,
            "completion_tokens": 12,
            "total_tokens": 21,
        },
    }


def test_function_tool_choice_and_parallel_intent_translate_to_chat() -> None:
    request = {
        "input": "Weather in Paris?",
        "tools": [WEATHER_TOOL],
        "tool_choice": {"type": "function", "name": "get_weather"},
        "parallel_tool_calls": False,
    }

    translated = to_chat_completions(request)

    assert translated == {
        "messages": [{"role": "user", "content": "Weather in Paris?"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather for a city.",
                    "parameters": WEATHER_TOOL["parameters"],
                    "strict": True,
                },
            }
        ],
        "tool_choice": {
            "type": "function",
            "function": {"name": "get_weather"},
        },
        "parallel_tool_calls": False,
    }
    assert request["tools"] == [WEATHER_TOOL]


@pytest.mark.parametrize("choice", ["auto", "none", "required"])
def test_string_tool_choice_translates_unchanged(choice: str) -> None:
    translated = to_chat_completions(
        {"input": "Weather?", "tools": [WEATHER_TOOL], "tool_choice": choice}
    )

    assert translated["tool_choice"] == choice


def test_stateless_function_result_replays_the_assistant_call_before_tool_output() -> None:
    request = {
        "input": [
            {"role": "user", "content": "Weather in Paris?"},
            _function_call(),
            {
                "type": "function_call_output",
                "call_id": "call_abc123",
                "output": "18C and sunny",
            },
        ]
    }

    translated = to_chat_completions(request)

    assert translated["messages"] == [
        {"role": "user", "content": "Weather in Paris?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_abc123",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city": "Paris"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_abc123",
            "content": "18C and sunny",
        },
    ]


def test_parallel_function_calls_share_one_assistant_message_and_keep_arguments_exact() -> None:
    request = {
        "input": [
            {"role": "user", "content": "Weather in Paris and Rome?"},
            _function_call(),
            _function_call("call_def456", city="Rome"),
            {
                "type": "function_call_output",
                "call_id": "call_abc123",
                "output": "18C",
            },
            {
                "type": "function_call_output",
                "call_id": "call_def456",
                "output": "24C",
            },
        ]
    }

    translated = to_chat_completions(request)

    assert translated["messages"][1] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": '{"city": "Paris"}',
                },
            },
            {
                "id": "call_def456",
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": '{"city": "Rome"}',
                },
            },
        ],
    }
    assert [message["tool_call_id"] for message in translated["messages"][2:]] == [
        "call_abc123",
        "call_def456",
    ]


def test_tool_only_chat_response_becomes_a_stable_function_call_item() -> None:
    chat_response = _chat_tool_response()

    first = to_responses(chat_response)
    second = to_responses(chat_response)

    assert first == second
    assert first["output_text"] == ""
    assert first["output"] == [
        {
            "id": "fc_call_abc123",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_abc123",
            "name": "get_weather",
            "arguments": '{"city": "Paris"}',
        }
    ]
    assert first["usage"] == {
        "input_tokens": 9,
        "output_tokens": 12,
        "total_tokens": 21,
    }


def test_mixed_chat_response_keeps_text_then_parallel_function_calls() -> None:
    chat_response = _chat_tool_response(
        content="I will check both cities.",
        tool_calls=[
            {
                "id": "call_abc123",
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": '{"city": "Paris"}',
                },
            },
            {
                "id": "call_def456",
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": '{"city": "Rome"}',
                },
            },
        ],
    )

    response = to_responses(chat_response)

    assert response["output_text"] == "I will check both cities."
    assert response["output"][0]["type"] == "message"
    assert [item["id"] for item in response["output"][1:]] == [
        "fc_call_abc123",
        "fc_call_def456",
    ]
    assert [item["call_id"] for item in response["output"][1:]] == [
        "call_abc123",
        "call_def456",
    ]


@pytest.mark.parametrize(
    "tool_call",
    [
        {"type": "function", "function": {"name": "get_weather", "arguments": "{}"}},
        {"id": "call_x", "type": "function", "function": {"arguments": "{}"}},
        {
            "id": "call_x",
            "type": "function",
            "function": {"name": "get_weather", "arguments": {}},
        },
    ],
)
def test_malformed_upstream_tool_call_fails_closed(tool_call: dict[str, object]) -> None:
    with pytest.raises(UpstreamUnavailable, match="malformed tool call"):
        to_responses(_chat_tool_response(tool_calls=[tool_call]))


def test_non_function_upstream_tool_call_fails_closed() -> None:
    response = _chat_tool_response()
    response["choices"][0]["message"]["tool_calls"][0]["type"] = "custom"  # type: ignore[index]

    with pytest.raises(UpstreamUnavailable, match="malformed tool call"):
        to_responses(response)


def test_duplicate_upstream_tool_call_ids_fail_closed() -> None:
    tool_call: dict[str, object] = {
        "id": "call_duplicate",
        "type": "function",
        "function": {"name": "get_weather", "arguments": "{}"},
    }

    with pytest.raises(UpstreamUnavailable, match="duplicate tool call"):
        to_responses(_chat_tool_response(tool_calls=[tool_call, tool_call]))


@pytest.mark.parametrize("tool_calls", [{}, "tool", 1])
def test_non_list_upstream_tool_calls_fail_closed(tool_calls: object) -> None:
    response = _chat_tool_response()
    response["choices"][0]["message"]["tool_calls"] = tool_calls  # type: ignore[index]

    with pytest.raises(UpstreamUnavailable, match="malformed tool call response"):
        to_responses(response)


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
def test_incomplete_upstream_tool_response_fails_closed_and_carries_usage(
    finish_reason: str,
) -> None:
    response = _chat_tool_response()
    response["choices"][0]["finish_reason"] = finish_reason  # type: ignore[index]

    with pytest.raises(UpstreamUnavailable, match="incomplete") as raised:
        to_responses(response)

    assert isinstance(raised.value, UpstreamResponseInvalid)
    assert raised.value.billable_response == {"usage": response["usage"]}


@pytest.mark.parametrize(
    "usage",
    ["bad", {"prompt_tokens": "many", "completion_tokens": 12}],
)
def test_malformed_upstream_usage_fails_closed_with_a_safe_billing_view(usage: object) -> None:
    response = _chat_tool_response()
    response["usage"] = usage

    with pytest.raises(UpstreamResponseInvalid, match="usage") as raised:
        to_responses(response)

    assert raised.value.billable_response == {
        "usage": {"completion_tokens": 12} if isinstance(usage, dict) else {}
    }


def test_large_parallel_tool_replay_preserves_all_calls() -> None:
    calls = [_function_call(f"call_{index}", city=str(index)) for index in range(2_000)]
    outputs = [
        {
            "type": "function_call_output",
            "call_id": f"call_{index}",
            "output": str(index),
        }
        for index in range(2_000)
    ]

    translated = to_chat_completions({"input": [*calls, *outputs]})

    tool_calls = translated["messages"][0]["tool_calls"]
    assert len(tool_calls) == 2_000
    assert tool_calls[0]["id"] == "call_0"
    assert tool_calls[-1]["id"] == "call_1999"
