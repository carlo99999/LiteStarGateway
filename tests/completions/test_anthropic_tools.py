"""Faithful non-streaming Chat tool contracts for the Anthropic adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from litestar_gateway.domain.entities import Model, ModelType, Provider
from litestar_gateway.domain.exceptions import UpstreamResponseInvalid
from litestar_gateway.infrastructure.llm.anthropic_adapter import (
    _response_tool_contract,
    anthropic_event_to_delta,
    from_anthropic_response,
    to_anthropic_request,
)


def _model() -> Model:
    return Model(
        id=uuid4(),
        team_id=uuid4(),
        name="m",
        provider=Provider.ANTHROPIC,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="claude-sonnet-4-5",
        params={},
        api_version=None,
        input_cost_per_token=None,
        output_cost_per_token=None,
        enabled=True,
        created_at=datetime.now(UTC),
    )


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
            "strict": True,
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Get local time for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    },
]


def test_request_maps_tools_parallel_replay_and_exact_call_ids() -> None:
    request = to_anthropic_request(
        {
            "messages": [
                {"role": "user", "content": "Weather and time in Paris?"},
                {
                    "role": "assistant",
                    "content": "I'll check both.",
                    "tool_calls": [
                        {
                            "id": "toolu_weather",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city":"Paris"}',
                            },
                        },
                        {
                            "id": "toolu_time",
                            "type": "function",
                            "function": {
                                "name": "get_time",
                                "arguments": '{"city":"Paris"}',
                            },
                        },
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "toolu_time",
                    "content": "14:30",
                },
                {
                    "role": "tool",
                    "tool_call_id": "toolu_weather",
                    "content": "18C and sunny",
                },
            ],
            "tools": TOOLS,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        },
        _model(),
    )

    assert request["tools"] == [
        {
            "name": "get_weather",
            "description": "Get weather for a city.",
            "input_schema": TOOLS[0]["function"]["parameters"],
            "strict": True,
        },
        {
            "name": "get_time",
            "description": "Get local time for a city.",
            "input_schema": TOOLS[1]["function"]["parameters"],
        },
    ]
    assert request["tool_choice"] == {
        "type": "auto",
        "disable_parallel_tool_use": True,
    }
    assert request["messages"] == [
        {"role": "user", "content": "Weather and time in Paris?"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll check both."},
                {
                    "type": "tool_use",
                    "id": "toolu_weather",
                    "name": "get_weather",
                    "input": {"city": "Paris"},
                },
                {
                    "type": "tool_use",
                    "id": "toolu_time",
                    "name": "get_time",
                    "input": {"city": "Paris"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_time",
                    "content": "14:30",
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_weather",
                    "content": "18C and sunny",
                },
            ],
        },
    ]


@pytest.mark.parametrize(
    ("choice", "parallel", "expected"),
    [
        ("auto", True, {"type": "auto", "disable_parallel_tool_use": False}),
        ("required", None, {"type": "any"}),
        ("none", None, {"type": "none"}),
        (
            {"type": "function", "function": {"name": "get_weather"}},
            False,
            {
                "type": "tool",
                "name": "get_weather",
                "disable_parallel_tool_use": True,
            },
        ),
    ],
)
def test_request_maps_every_tool_choice(
    choice: object, parallel: bool | None, expected: dict[str, object]
) -> None:
    payload: dict[str, object] = {
        "messages": [{"role": "user", "content": "Weather?"}],
        "tools": TOOLS,
        "tool_choice": choice,
    }
    if parallel is not None:
        payload["parallel_tool_calls"] = parallel

    translated = to_anthropic_request(payload, _model())

    assert translated["tool_choice"] == expected


def test_response_preserves_text_parallel_calls_ids_and_usage() -> None:
    response = from_anthropic_response(
        {
            "id": "msg_123",
            "model": "claude-sonnet-4-5",
            "content": [
                {"type": "text", "text": "I'll check both."},
                {
                    "type": "tool_use",
                    "id": "toolu_weather",
                    "name": "get_weather",
                    "input": {"city": "Paris"},
                },
                {
                    "type": "tool_use",
                    "id": "toolu_time",
                    "name": "get_time",
                    "input": {"city": "Paris"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 9, "output_tokens": 11},
        },
        expected_tool_names={"get_weather", "get_time"},
    )

    choice = response["choices"][0]
    assert choice["message"]["content"] == "I'll check both."
    assert choice["message"]["tool_calls"] == [
        {
            "id": "toolu_weather",
            "type": "function",
            "function": {
                "name": "get_weather",
                "arguments": json.dumps({"city": "Paris"}, separators=(",", ":"), sort_keys=True),
            },
        },
        {
            "id": "toolu_time",
            "type": "function",
            "function": {
                "name": "get_time",
                "arguments": json.dumps({"city": "Paris"}, separators=(",", ":"), sort_keys=True),
            },
        },
    ]
    assert choice["finish_reason"] == "tool_calls"
    assert response["usage"] == {
        "prompt_tokens": 9,
        "completion_tokens": 11,
        "total_tokens": 20,
    }


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"messages": [], "tools": TOOLS, "tool_choice": "none"},
            (set(), False, 0, None),
        ),
        (
            {
                "messages": [],
                "tools": TOOLS,
                "tool_choice": {
                    "type": "function",
                    "function": {"name": "get_weather"},
                },
            },
            ({"get_weather"}, True, None, None),
        ),
        (
            {"messages": [], "tools": TOOLS, "tool_choice": "required"},
            ({"get_weather", "get_time"}, True, None, None),
        ),
        (
            {"messages": [], "tools": TOOLS, "parallel_tool_calls": False},
            ({"get_weather", "get_time"}, False, 1, None),
        ),
    ],
)
def test_response_contract_reflects_choice_and_parallel_constraints(
    payload: dict[str, object],
    expected: tuple[set[str], bool, int | None, str | None],
) -> None:
    assert _response_tool_contract(payload, _model()) == expected


def test_structured_output_tool_stays_json_content_not_a_function_call() -> None:
    response = from_anthropic_response(
        {
            "id": "msg_123",
            "model": "claude-sonnet-4-5",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_structured",
                    "name": "answer",
                    "input": {"answer": 42},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 3, "output_tokens": 5},
        },
        structured_tool_name="answer",
    )

    choice = response["choices"][0]
    assert json.loads(choice["message"]["content"]) == {"answer": 42}
    assert "tool_calls" not in choice["message"]
    assert choice["finish_reason"] == "stop"


@pytest.mark.parametrize(
    "message",
    [
        {
            "id": "msg_bad_usage",
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "usage": "malformed",
        },
        {
            "id": "msg_bad_text",
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": 123}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 4, "output_tokens": 6},
        },
        {
            "id": "msg_negative_usage",
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": -4, "output_tokens": 6},
        },
    ],
)
def test_response_rejects_malformed_content_or_usage(message: dict[str, object]) -> None:
    with pytest.raises(UpstreamResponseInvalid):
        from_anthropic_response(message)


def test_response_maps_safety_refusal_but_rejects_incomplete_pause_turn() -> None:
    refusal = from_anthropic_response(
        {
            "id": "msg_refusal",
            "model": "claude-sonnet-4-5",
            "content": [{"type": "text", "text": "I cannot help with that."}],
            "stop_reason": "refusal",
            "usage": {"input_tokens": 4, "output_tokens": 0},
        },
        expected_tool_names=set(),
        require_tool_call=True,
    )

    assert refusal["choices"][0]["finish_reason"] == "content_filter"
    assert refusal["choices"][0]["message"]["content"] == ""
    assert refusal["usage"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }

    with pytest.raises(UpstreamResponseInvalid, match="incomplete"):
        from_anthropic_response(
            {
                "id": "msg_pause",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "Partial turn"}],
                "stop_reason": "pause_turn",
                "usage": {"input_tokens": 4, "output_tokens": 6},
            },
            expected_tool_names=set(),
        )


def test_response_discards_but_bills_mid_output_refusal() -> None:
    refusal = from_anthropic_response(
        {
            "id": "msg_mid_refusal",
            "model": "claude-sonnet-4-5",
            "content": [
                {"type": "text", "text": "Discard this partial output."},
                {"type": "thinking", "thinking": "also discard"},
            ],
            "stop_reason": "refusal",
            "usage": {"input_tokens": 4, "output_tokens": 2},
        },
        expected_tool_names={"get_weather"},
        require_tool_call=True,
        structured_tool_name="answer",
    )

    assert refusal["choices"][0]["finish_reason"] == "content_filter"
    assert refusal["choices"][0]["message"]["content"] == ""
    assert "tool_calls" not in refusal["choices"][0]["message"]
    assert refusal["usage"] == {
        "prompt_tokens": 4,
        "completion_tokens": 2,
        "total_tokens": 6,
    }


def test_stream_stop_reason_maps_refusal_and_rejects_pause_turn() -> None:
    delta, finish = anthropic_event_to_delta(
        {
            "type": "message_delta",
            "delta": {"stop_reason": "refusal"},
        }
    )
    assert delta == {}
    assert finish == "content_filter"

    with pytest.raises(UpstreamResponseInvalid, match="incomplete"):
        anthropic_event_to_delta(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "pause_turn"},
            }
        )


def test_response_rejects_undeclared_tool_name_and_preserves_billable_usage() -> None:
    with pytest.raises(UpstreamResponseInvalid) as exc:
        from_anthropic_response(
            {
                "id": "msg_wrong_tool",
                "model": "claude-sonnet-4-5",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_wrong",
                        "name": "undeclared",
                        "input": {},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 4, "output_tokens": 6},
            },
            expected_tool_names={"get_weather"},
        )

    assert exc.value.billable_response == {
        "usage": {
            "prompt_tokens": 4,
            "completion_tokens": 6,
            "total_tokens": 10,
        }
    }


def test_response_enforces_none_required_and_disabled_parallel_contracts() -> None:
    tool_call = {
        "type": "tool_use",
        "id": "toolu_weather",
        "name": "get_weather",
        "input": {},
    }
    base = {
        "id": "msg_contract",
        "model": "claude-sonnet-4-5",
        "usage": {"input_tokens": 4, "output_tokens": 6},
    }

    with pytest.raises(UpstreamResponseInvalid, match="tool call"):
        from_anthropic_response(
            {
                **base,
                "content": [tool_call],
                "stop_reason": "tool_use",
            },
            expected_tool_names=set(),
            max_tool_calls=0,
        )

    with pytest.raises(UpstreamResponseInvalid, match="required"):
        from_anthropic_response(
            {
                **base,
                "content": [{"type": "text", "text": "No call"}],
                "stop_reason": "end_turn",
            },
            expected_tool_names={"get_weather"},
            require_tool_call=True,
        )

    with pytest.raises(UpstreamResponseInvalid, match="parallel"):
        from_anthropic_response(
            {
                **base,
                "content": [
                    tool_call,
                    {
                        **tool_call,
                        "id": "toolu_weather_2",
                    },
                ],
                "stop_reason": "tool_use",
            },
            expected_tool_names={"get_weather"},
            max_tool_calls=1,
        )


def test_structured_output_rejects_a_different_forced_tool_name() -> None:
    with pytest.raises(UpstreamResponseInvalid, match="structured output"):
        from_anthropic_response(
            {
                "id": "msg_wrong_structured_tool",
                "model": "claude-sonnet-4-5",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_wrong",
                        "name": "different",
                        "input": {"answer": 42},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 3, "output_tokens": 5},
            },
            structured_tool_name="answer",
        )
