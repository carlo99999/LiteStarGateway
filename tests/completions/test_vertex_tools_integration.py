from __future__ import annotations

import base64

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_501_NOT_IMPLEMENTED,
    HTTP_502_BAD_GATEWAY,
)
from litestar.testing import AsyncTestClient

from .conftest import (
    VERTEX_VALUES,
    FakeGenaiClient,
    _bearer,
    _FakeGeminiModels,
    _patch,
    _Result,
    _setup,
    _setup_team,
    _team_usage,
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


async def test_vertex_chat_two_turn_tool_loop_preserves_signature_and_billing(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def generate_content(self: _FakeGeminiModels, **kwargs: object) -> _Result:
        calls.append(kwargs)
        contents = kwargs["contents"]
        assert isinstance(contents, list)
        has_result = any(
            isinstance(content, dict)
            and any(
                isinstance(part, dict) and part.get("function_response") is not None
                for part in content.get("parts", [])
            )
            for content in contents
        )
        parts = (
            [{"text": "It is sunny."}]
            if has_result
            else [
                {
                    "function_call": {
                        "id": "call_a",
                        "name": "weather",
                        "args": {"city": "Rome"},
                    },
                    "thought_signature": SIGNATURE,
                }
            ]
        )
        return _Result(
            {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": parts},
                        "finish_reason": "STOP",
                    }
                ],
                "usage_metadata": {
                    "prompt_token_count": 4,
                    "candidates_token_count": 2,
                    "total_token_count": 6,
                },
                "model_version": "gemini-3-pro",
                "response_id": f"response-{len(calls)}",
            }
        )

    _patch(monkeypatch)
    monkeypatch.setattr(_FakeGeminiModels, "generate_content", generate_content)
    api_key, team, admin = await _setup_team(
        client,
        provider="vertex_ai",
        values=VERTEX_VALUES,
        provider_model_id="gemini-3-pro",
        input_cost_per_token=0.01,
        output_cost_per_token=0.02,
    )
    first = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [{"role": "user", "content": "Weather in Rome?"}],
            "tools": TOOLS,
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        },
        headers=_bearer(api_key),
    )

    assert first.status_code == HTTP_200_OK, first.text
    first_body = first.json()
    tool_call = first_body["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["id"] == "call_a"
    assert tool_call["extra_content"]["google"]["thought_signature"] == SIGNATURE_B64

    second = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [
                {"role": "user", "content": "Weather in Rome?"},
                first_body["choices"][0]["message"],
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": '{"temperature":23,"condition":"sunny"}',
                },
            ],
            "tools": TOOLS,
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        },
        headers=_bearer(api_key),
    )

    assert second.status_code == HTTP_200_OK, second.text
    assert second.json()["choices"][0]["message"]["content"] == "It is sunny."
    replay_part = calls[1]["contents"][1]["parts"][0]  # type: ignore[index]
    assert replay_part["thought_signature"] == SIGNATURE
    assert calls[1]["contents"][2]["parts"] == [  # type: ignore[index]
        {
            "function_response": {
                "id": "call_a",
                "name": "weather",
                "response": {"temperature": 23, "condition": "sunny"},
            }
        }
    ]
    usage = await _team_usage(client, team, admin)
    assert usage[0]["calls"] == 2
    assert usage[0]["prompt_tokens"] == 8
    assert usage[0]["completion_tokens"] == 4
    assert usage[0]["cost"] == pytest.approx(0.16)
    assert FakeGenaiClient.closed is True


async def test_vertex_streaming_tools_and_responses_tools_remain_fail_closed(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client,
        provider="vertex_ai",
        values=VERTEX_VALUES,
        provider_model_id="gemini-3-pro",
    )

    chat = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [{"role": "user", "content": "Weather?"}],
            "tools": TOOLS,
            "stream": True,
        },
        headers=_bearer(api_key),
    )
    responses = await client.post(
        "/v1/responses",
        json={
            "model": "m",
            "input": "Weather?",
            "tools": [
                {
                    "type": "function",
                    "name": "weather",
                    "parameters": {"type": "object"},
                }
            ],
        },
        headers=_bearer(api_key),
    )

    assert chat.status_code == HTTP_501_NOT_IMPLEMENTED
    assert responses.status_code == HTTP_501_NOT_IMPLEMENTED
    assert FakeGenaiClient.last_kwargs == {}


async def test_vertex_malformed_billable_tool_response_settles_once(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def malformed(self: _FakeGeminiModels, **kwargs: object) -> _Result:
        return _Result(
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "function_call": {
                                        "id": "call_a",
                                        "name": "not_declared",
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
                    "prompt_token_count": 3,
                    "candidates_token_count": 5,
                    "total_token_count": 8,
                },
                "model_version": "gemini-3-pro",
                "response_id": "response-bad",
            }
        )

    _patch(monkeypatch)
    monkeypatch.setattr(_FakeGeminiModels, "generate_content", malformed)
    api_key, team, admin = await _setup_team(
        client,
        provider="vertex_ai",
        values=VERTEX_VALUES,
        provider_model_id="gemini-3-pro",
        input_cost_per_token=0.01,
        output_cost_per_token=0.02,
    )

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [{"role": "user", "content": "Weather?"}],
            "tools": TOOLS,
        },
        headers=_bearer(api_key),
    )

    assert response.status_code == HTTP_502_BAD_GATEWAY
    usage = await _team_usage(client, team, admin)
    assert usage[0]["calls"] == 1
    assert usage[0]["prompt_tokens"] == 3
    assert usage[0]["completion_tokens"] == 5
    assert usage[0]["cost"] == pytest.approx(0.13)
