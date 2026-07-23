"""Non-streaming Responses API (`/v1/responses`), per provider."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_501_NOT_IMPLEMENTED, HTTP_502_BAD_GATEWAY
from litestar.testing import AsyncTestClient

from litestar_gateway.config import DEFAULT_MAX_RETRIES, DEFAULT_REQUEST_TIMEOUT

from .conftest import (
    ANTHROPIC_VALUES,
    AZURE_VALUES,
    DATABRICKS_VALUES,
    VERTEX_VALUES,
    FakeAnthropic,
    FakeClient,
    FakeGenaiClient,
    _bearer,
    _patch,
    _setup,
    _setup_team,
    _team_usage,
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


async def test_openai_responses(client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch)
    api_key = await _setup(client)
    resp = await client.post(
        "/v1/responses", json={"model": "m", "input": "hi"}, headers=_bearer(api_key)
    )
    assert resp.status_code == HTTP_200_OK


async def test_openai_responses_preserves_native_only_fields(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(client)
    resp = await client.post(
        "/v1/responses",
        json={
            "model": "m",
            "input": "hi",
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": "tenant-request",
            "safety_identifier": "actor-123",
            "top_logprobs": 3,
            "truncation": "auto",
        },
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeClient.last_kwargs["include"] == ["reasoning.encrypted_content"]
    assert FakeClient.last_kwargs["prompt_cache_key"] == "tenant-request"
    assert FakeClient.last_kwargs["safety_identifier"] == "actor-123"
    assert FakeClient.last_kwargs["top_logprobs"] == 3
    assert FakeClient.last_kwargs["truncation"] == "auto"
    assert FakeClient.last_kwargs["store"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("background", True),
        ("service_tier", "flex"),
        ("store", True),
        ("previous_response_id", "resp_123"),
        ("conversation", "conv_123"),
        ("prompt", {"id": "pmpt_123"}),
        ("prompt_cache_retention", "24h"),
    ],
)
async def test_openai_responses_rejects_unmetered_or_cross_tenant_state_before_provider(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(client)
    FakeClient.last_kwargs = {}

    resp = await client.post(
        "/v1/responses",
        json={"model": "m", "input": "hi", field: value},
        headers=_bearer(api_key),
    )

    assert resp.status_code == HTTP_501_NOT_IMPLEMENTED
    assert field in resp.json()["error"]["message"]
    assert resp.json()["error"]["code"] == "UnsupportedOperation"
    assert FakeClient.last_kwargs == {}


@pytest.mark.parametrize(
    "payload,match",
    [
        ({"tools": [{"type": "web_search"}]}, "tools[0].web_search"),
        (
            {
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_file", "file_id": "file_other_tenant"}],
                    }
                ]
            },
            "file_id",
        ),
        ({"input": [{"id": "item_other_tenant"}]}, "input[0].id"),
    ],
)
async def test_openai_responses_rejects_hosted_tools_and_provider_resource_ids(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict,
    match: str,
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(client)
    FakeClient.last_kwargs = {}

    resp = await client.post(
        "/v1/responses",
        json={"model": "m", "input": "hi", **payload},
        headers=_bearer(api_key),
    )

    assert resp.status_code == HTTP_501_NOT_IMPLEMENTED
    assert match in resp.json()["error"]["message"]
    assert FakeClient.last_kwargs == {}


async def test_azure_chat_and_responses(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="azure_openai", values=AZURE_VALUES, provider_model_id="my-deploy"
    )
    chat = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": []},
        headers=_bearer(api_key),
    )
    assert chat.status_code == HTTP_200_OK
    # Azure client built with the endpoint + version from the credential.
    assert FakeClient.last_init["azure_endpoint"] == AZURE_VALUES["api_base"]
    assert FakeClient.last_init["api_version"] == AZURE_VALUES["api_version"]
    assert FakeClient.last_kwargs["model"] == "my-deploy"  # deployment name

    resp = await client.post(
        "/v1/responses", json={"model": "m", "input": "hi"}, headers=_bearer(api_key)
    )
    assert resp.status_code == HTTP_200_OK


async def test_databricks_chat_works(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="databricks", values=DATABRICKS_VALUES, provider_model_id="my-endpoint"
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": []},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeClient.last_init["base_url"] == DATABRICKS_VALUES["api_base"]


async def test_databricks_responses_emulated_over_chat(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Databricks has no native Responses API → emulated via chat.completions.
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="databricks", values=DATABRICKS_VALUES, provider_model_id="my-endpoint"
    )
    resp = await client.post(
        "/v1/responses",
        json={"model": "m", "instructions": "be brief", "input": "hi"},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    body = resp.json()
    assert body["object"] == "response"
    assert body["output_text"] == "hello"
    assert body["model"] == "my-endpoint"
    # The Responses input was translated into chat messages (system + user).
    assert FakeClient.last_kwargs["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]


async def test_databricks_responses_rejects_hosted_tools_before_provider(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="databricks", values=DATABRICKS_VALUES, provider_model_id="my-endpoint"
    )
    FakeClient.last_kwargs = {}

    resp = await client.post(
        "/v1/responses",
        json={
            "model": "m",
            "input": "hi",
            "tools": [{"type": "web_search"}],
        },
        headers=_bearer(api_key),
    )

    assert resp.status_code == HTTP_501_NOT_IMPLEMENTED
    assert "tools[0].type" in resp.json()["error"]["message"]
    assert resp.json()["error"]["code"] == "UnsupportedOperation"
    assert FakeClient.last_kwargs == {}


async def test_databricks_responses_non_streaming_tool_loop_is_faithful_and_billed_once_per_call(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key, team, admin = await _setup_team(
        client,
        provider="databricks",
        values=DATABRICKS_VALUES,
        provider_model_id="my-endpoint",
        input_cost_per_token=0.01,
        output_cost_per_token=0.02,
    )

    first = await client.post(
        "/v1/responses",
        json={
            "model": "m",
            "input": "Weather in Paris?",
            "tools": [WEATHER_TOOL],
            "tool_choice": {"type": "function", "name": "get_weather"},
            "parallel_tool_calls": False,
        },
        headers=_bearer(api_key),
    )

    assert first.status_code == HTTP_200_OK
    first_body = first.json()
    assert first_body["output"] == [
        {
            "id": "fc_call_abc123",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_abc123",
            "name": "get_weather",
            "arguments": '{"city": "Paris"}',
        }
    ]
    assert first_body["output_text"] == ""
    assert FakeClient.last_kwargs["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather for a city.",
                "parameters": WEATHER_TOOL["parameters"],
                "strict": True,
            },
        }
    ]
    assert FakeClient.last_kwargs["tool_choice"] == {
        "type": "function",
        "function": {"name": "get_weather"},
    }
    assert FakeClient.last_kwargs["parallel_tool_calls"] is False
    assert FakeClient.calls == 1

    first_usage = await _team_usage(client, team, admin)
    assert len(first_usage) == 1
    assert first_usage[0]["calls"] == 1
    assert first_usage[0]["prompt_tokens"] == 9
    assert first_usage[0]["completion_tokens"] == 12
    assert first_usage[0]["cost"] == pytest.approx(0.33)

    second = await client.post(
        "/v1/responses",
        json={
            "model": "m",
            "input": [
                {"role": "user", "content": "Weather in Paris?"},
                first_body["output"][0],
                {
                    "type": "function_call_output",
                    "call_id": "call_abc123",
                    "output": "18C and sunny",
                },
            ],
            "tools": [WEATHER_TOOL],
        },
        headers=_bearer(api_key),
    )

    assert second.status_code == HTTP_200_OK
    assert second.json()["output_text"] == "It is sunny in Paris."
    assert FakeClient.last_kwargs["messages"] == [
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
    assert FakeClient.calls == 2

    final_usage = await _team_usage(client, team, admin)
    assert len(final_usage) == 1
    assert final_usage[0]["calls"] == 2
    assert final_usage[0]["prompt_tokens"] == 14
    assert final_usage[0]["completion_tokens"] == 19
    assert final_usage[0]["cost"] == pytest.approx(0.52)


async def test_anthropic_responses_non_streaming_tool_loop_is_faithful_and_billed_once_per_call(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key, team, admin = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-sonnet-4-5",
        input_cost_per_token=0.01,
        output_cost_per_token=0.02,
    )

    first = await client.post(
        "/v1/responses",
        json={
            "model": "m",
            "input": "Weather in Paris?",
            "tools": [WEATHER_TOOL],
            "tool_choice": {"type": "function", "name": "get_weather"},
            "parallel_tool_calls": False,
        },
        headers=_bearer(api_key),
    )

    assert first.status_code == HTTP_200_OK
    first_body = first.json()
    assert first_body["output"] == [
        {
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "type": "output_text",
                    "text": "I'll check.",
                    "annotations": [],
                }
            ],
        },
        {
            "id": "fc_toolu-weather",
            "type": "function_call",
            "status": "completed",
            "call_id": "toolu-weather",
            "name": "get_weather",
            "arguments": '{"city":"Paris"}',
        },
    ]
    assert first_body["output_text"] == "I'll check."
    assert FakeAnthropic.last_kwargs["tools"] == [
        {
            "name": "get_weather",
            "description": "Get weather for a city.",
            "input_schema": WEATHER_TOOL["parameters"],
            "strict": True,
        }
    ]
    assert FakeAnthropic.last_kwargs["tool_choice"] == {
        "type": "tool",
        "name": "get_weather",
        "disable_parallel_tool_use": True,
    }
    assert FakeAnthropic.calls == 1

    first_usage = await _team_usage(client, team, admin)
    assert len(first_usage) == 1
    assert first_usage[0]["calls"] == 1
    assert first_usage[0]["prompt_tokens"] == 4
    assert first_usage[0]["completion_tokens"] == 6
    assert first_usage[0]["cost"] == pytest.approx(0.16)

    second = await client.post(
        "/v1/responses",
        json={
            "model": "m",
            "input": [
                {"role": "user", "content": "Weather in Paris?"},
                first_body["output"][1],
                {
                    "type": "function_call_output",
                    "call_id": "toolu-weather",
                    "output": "18C and sunny",
                },
            ],
            "tools": [WEATHER_TOOL],
        },
        headers=_bearer(api_key),
    )

    assert second.status_code == HTTP_200_OK
    assert second.json()["output_text"] == "hi there"
    assert FakeAnthropic.last_kwargs["messages"] == [
        {"role": "user", "content": "Weather in Paris?"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu-weather",
                    "name": "get_weather",
                    "input": {"city": "Paris"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu-weather",
                    "content": "18C and sunny",
                }
            ],
        },
    ]
    assert FakeAnthropic.calls == 2

    final_usage = await _team_usage(client, team, admin)
    assert len(final_usage) == 1
    assert final_usage[0]["calls"] == 2
    assert final_usage[0]["prompt_tokens"] == 7
    assert final_usage[0]["completion_tokens"] == 11
    assert final_usage[0]["cost"] == pytest.approx(0.29)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "input": "Weather in Paris?",
            "tools": [WEATHER_TOOL],
            "stream": True,
        },
        {
            "input": [
                {
                    "type": "function_call",
                    "call_id": "toolu-weather",
                    "name": "get_weather",
                    "arguments": '{"city":NaN}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "toolu-weather",
                    "output": "sunny",
                },
            ],
            "tools": [WEATHER_TOOL],
        },
        {
            "input": [
                {
                    "type": "function_call",
                    "call_id": "toolu-weather",
                    "name": "undeclared",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "toolu-weather",
                    "output": "sunny",
                },
            ],
            "tools": [WEATHER_TOOL],
        },
    ],
)
async def test_anthropic_responses_rejects_unfaithful_tool_requests_before_provider_and_billing(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    _patch(monkeypatch)
    api_key, team, admin = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-sonnet-4-5",
    )

    response = await client.post(
        "/v1/responses",
        json={"model": "m", **payload},
        headers=_bearer(api_key),
    )

    assert response.status_code == HTTP_501_NOT_IMPLEMENTED
    assert FakeAnthropic.calls == 0
    assert await _team_usage(client, team, admin) == []


async def test_anthropic_responses_rejects_configured_response_format_with_tools_before_billing(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    api_key, team, admin = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-sonnet-4-5",
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

    response = await client.post(
        "/v1/responses",
        json={"model": "m", "input": "Weather?", "tools": [WEATHER_TOOL]},
        headers=_bearer(api_key),
    )

    assert response.status_code == HTTP_501_NOT_IMPLEMENTED
    assert FakeAnthropic.calls == 0
    assert await _team_usage(client, team, admin) == []


async def test_malformed_anthropic_tool_response_fails_closed_but_is_billed(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    api_key, team, admin = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-sonnet-4-5",
        input_cost_per_token=0.01,
        output_cost_per_token=0.02,
    )

    async def malformed_tool_response(_client: FakeAnthropic, **kwargs: object) -> object:
        return SimpleNamespace(
            model_dump=lambda: {
                "id": "msg-malformed",
                "model": kwargs.get("model"),
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu-bad",
                        "name": "get_weather",
                        "input": "not-an-object",
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 4, "output_tokens": 6},
            }
        )

    monkeypatch.setattr(FakeAnthropic, "create", malformed_tool_response)
    response = await client.post(
        "/v1/responses",
        json={"model": "m", "input": "Weather?", "tools": [WEATHER_TOOL]},
        headers=_bearer(api_key),
    )

    assert response.status_code == HTTP_502_BAD_GATEWAY
    usage = await _team_usage(client, team, admin)
    assert usage[0]["calls"] == 1
    assert usage[0]["prompt_tokens"] == 4
    assert usage[0]["completion_tokens"] == 6
    assert usage[0]["cost"] == pytest.approx(0.16)


async def test_anthropic_pre_output_refusal_is_empty_success_and_not_billed(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    api_key, team, admin = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-sonnet-4-5",
        input_cost_per_token=0.01,
        output_cost_per_token=0.02,
    )

    async def refusal(_client: FakeAnthropic, **kwargs: object) -> object:
        FakeAnthropic.calls += 1
        return SimpleNamespace(
            model_dump=lambda: {
                "id": "msg-refusal",
                "model": kwargs.get("model"),
                "content": [{"type": "text", "text": "discard me"}],
                "stop_reason": "refusal",
                "usage": {"input_tokens": 4, "output_tokens": 0},
            }
        )

    monkeypatch.setattr(FakeAnthropic, "create", refusal)
    response = await client.post(
        "/v1/responses",
        json={
            "model": "m",
            "input": "Weather?",
            "tools": [WEATHER_TOOL],
            "tool_choice": "required",
        },
        headers=_bearer(api_key),
    )

    assert response.status_code == HTTP_200_OK
    assert response.json()["output_text"] == ""
    settled = await _team_usage(client, team, admin)
    assert settled[0]["calls"] == 1
    assert settled[0]["prompt_tokens"] == 0
    assert settled[0]["completion_tokens"] == 0
    assert settled[0]["cost"] == 0


@pytest.mark.parametrize(
    ("content", "usage", "authoritative_usage"),
    [
        (
            [{"type": "text", "text": 123}],
            {"input_tokens": 4, "output_tokens": 6},
            True,
        ),
        (
            [{"type": "text", "text": "done"}],
            "malformed",
            False,
        ),
        (
            [{"type": "text", "text": "done"}],
            {"input_tokens": -4, "output_tokens": 6},
            False,
        ),
    ],
)
async def test_malformed_anthropic_content_or_usage_fails_closed_and_never_bills_negative(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
    content: list[dict[str, object]],
    usage: object,
    authoritative_usage: bool,
) -> None:
    _patch(monkeypatch)
    api_key, team, admin = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-sonnet-4-5",
        input_cost_per_token=0.01,
        output_cost_per_token=0.02,
    )

    async def malformed_response(_client: FakeAnthropic, **kwargs: object) -> object:
        FakeAnthropic.calls += 1
        return SimpleNamespace(
            model_dump=lambda: {
                "id": "msg-malformed",
                "model": kwargs.get("model"),
                "content": content,
                "stop_reason": "end_turn",
                "usage": usage,
            }
        )

    monkeypatch.setattr(FakeAnthropic, "create", malformed_response)
    response = await client.post(
        "/v1/responses",
        json={"model": "m", "input": "Hello"},
        headers=_bearer(api_key),
    )

    assert response.status_code == HTTP_502_BAD_GATEWAY
    assert FakeAnthropic.calls == 1
    settled = await _team_usage(client, team, admin)
    assert settled[0]["calls"] == 1
    assert settled[0]["prompt_tokens"] >= 0
    assert settled[0]["completion_tokens"] >= 0
    assert settled[0]["cost"] >= 0
    if authoritative_usage:
        assert settled[0]["prompt_tokens"] == 4
        assert settled[0]["completion_tokens"] == 6


@pytest.mark.parametrize("stream", [1, "true"])
async def test_databricks_responses_reject_non_boolean_stream_before_provider_and_billing(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
    stream: object,
) -> None:
    _patch(monkeypatch)
    api_key, team, admin = await _setup_team(
        client,
        provider="databricks",
        values=DATABRICKS_VALUES,
        provider_model_id="my-endpoint",
    )
    FakeClient.last_kwargs = {}

    response = await client.post(
        "/v1/responses",
        json={
            "model": "m",
            "input": "Weather in Paris?",
            "stream": stream,
            "tools": [WEATHER_TOOL],
        },
        headers=_bearer(api_key),
    )

    assert response.status_code == HTTP_501_NOT_IMPLEMENTED
    assert "stream" in response.json()["error"]["message"]
    assert FakeClient.last_kwargs == {}
    assert await _team_usage(client, team, admin) == []


async def test_malformed_databricks_tool_response_fails_closed_but_is_billed(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    api_key, team, admin = await _setup_team(
        client,
        provider="databricks",
        values=DATABRICKS_VALUES,
        provider_model_id="my-endpoint",
        input_cost_per_token=0.01,
        output_cost_per_token=0.02,
    )

    async def incomplete_tool_response(_client: FakeClient, **kwargs: object) -> object:
        FakeClient.calls += 1
        return SimpleNamespace(
            model_dump=lambda: {
                "id": "chatcmpl-incomplete",
                "object": "chat.completion",
                "created": 123,
                "model": kwargs.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_partial",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": '{"city": "Par',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "length",
                    }
                ],
                "usage": {
                    "prompt_tokens": 9,
                    "completion_tokens": 12,
                    "total_tokens": 21,
                },
            }
        )

    monkeypatch.setattr(FakeClient, "create", incomplete_tool_response)
    response = await client.post(
        "/v1/responses",
        json={
            "model": "m",
            "input": "Weather in Paris?",
            "tools": [WEATHER_TOOL],
        },
        headers=_bearer(api_key),
    )

    assert response.status_code == HTTP_502_BAD_GATEWAY
    usage = await _team_usage(client, team, admin)
    assert usage[0]["calls"] == 1
    assert usage[0]["prompt_tokens"] == 9
    assert usage[0]["completion_tokens"] == 12
    assert usage[0]["cost"] == pytest.approx(0.33)


async def test_malformed_databricks_usage_fails_closed_and_uses_billing_fallback(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    api_key, team, admin = await _setup_team(
        client,
        provider="databricks",
        values=DATABRICKS_VALUES,
        provider_model_id="my-endpoint",
        input_cost_per_token=0.01,
        output_cost_per_token=0.02,
    )

    async def malformed_usage_response(_client: FakeClient, **kwargs: object) -> object:
        return SimpleNamespace(
            model_dump=lambda: {
                "id": "chatcmpl-bad-usage",
                "object": "chat.completion",
                "created": 123,
                "model": kwargs.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "sunny"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": "bad",
            }
        )

    monkeypatch.setattr(FakeClient, "create", malformed_usage_response)
    response = await client.post(
        "/v1/responses",
        json={"model": "m", "input": "Weather in Paris?"},
        headers=_bearer(api_key),
    )

    assert response.status_code == HTTP_502_BAD_GATEWAY
    usage = await _team_usage(client, team, admin)
    assert usage[0]["calls"] == 1
    assert usage[0]["prompt_tokens"] > 0
    assert usage[0]["completion_tokens"] == 0
    assert usage[0]["cost"] > 0


async def test_ordinary_all_zero_usage_still_uses_billing_fallback(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    api_key, team, admin = await _setup_team(
        client,
        provider="databricks",
        values=DATABRICKS_VALUES,
        provider_model_id="my-endpoint",
        input_cost_per_token=0.01,
        output_cost_per_token=0.02,
    )

    async def zero_usage_response(_client: FakeClient, **kwargs: object) -> object:
        return SimpleNamespace(
            model_dump=lambda: {
                "id": "chatcmpl-zero-usage",
                "object": "chat.completion",
                "created": 123,
                "model": kwargs.get("model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }
        )

    monkeypatch.setattr(FakeClient, "create", zero_usage_response)
    response = await client.post(
        "/v1/responses",
        json={"model": "m", "input": "Weather in Paris?"},
        headers=_bearer(api_key),
    )

    assert response.status_code == HTTP_200_OK
    usage = await _team_usage(client, team, admin)
    assert usage[0]["calls"] == 1
    assert usage[0]["prompt_tokens"] > 0
    assert usage[0]["completion_tokens"] == 0
    assert usage[0]["cost"] > 0


@pytest.mark.parametrize(
    "payload,match",
    [
        (
            {"input": [{"role": "developer", "content": "Never disclose secrets"}]},
            "developer",
        ),
        (
            {"input": [{"role": "user", "content": "hi", "phase": "commentary"}]},
            "input.phase",
        ),
        ({"input": "hi", "instructions": 123}, "instructions"),
        ({"input": [{"role": [], "content": "hi"}]}, "input role"),
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
            "text.format.description",
        ),
    ],
)
async def test_anthropic_responses_rejects_nested_loss_before_provider(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict,
    match: str,
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-3-5-sonnet",
    )
    FakeAnthropic.last_kwargs = {}

    resp = await client.post(
        "/v1/responses",
        json={"model": "m", **payload},
        headers=_bearer(api_key),
    )

    assert resp.status_code == HTTP_501_NOT_IMPLEMENTED
    assert match in resp.json()["error"]["message"]
    assert FakeAnthropic.last_kwargs == {}


async def test_databricks_responses_rejects_model_tool_config_before_provider(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client,
        provider="databricks",
        values=DATABRICKS_VALUES,
        provider_model_id="my-endpoint",
        params_enforced={
            "tools": [{"type": "function", "function": {"name": "weather", "parameters": {}}}]
        },
    )
    FakeClient.last_kwargs = {}

    resp = await client.post(
        "/v1/responses",
        json={"model": "m", "input": "hi"},
        headers=_bearer(api_key),
    )

    assert resp.status_code == HTTP_501_NOT_IMPLEMENTED
    assert "configured model field(s): tools" in resp.json()["error"]["message"]
    assert FakeClient.last_kwargs == {}


async def test_anthropic_chat_translation(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-3-5-sonnet",
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hi"},
            ],
        },
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    # Client built with the gateway's resilience config (timeout + retries).
    assert FakeAnthropic.last_init["timeout"] == DEFAULT_REQUEST_TIMEOUT
    assert FakeAnthropic.last_init["max_retries"] == DEFAULT_MAX_RETRIES
    # Request: system extracted, only user/assistant in messages, max_tokens defaulted.
    assert FakeAnthropic.last_kwargs["system"] == "be brief"
    assert FakeAnthropic.last_kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert FakeAnthropic.last_kwargs["model"] == "claude-3-5-sonnet"
    assert FakeAnthropic.last_kwargs["max_tokens"] == 1024
    # Response: translated back to OpenAI chat shape.
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hi there"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 3
    assert body["usage"]["completion_tokens"] == 5


@pytest.mark.parametrize(
    ("stream", "params_enforced"),
    [
        (1, None),
        ("true", None),
        (True, {"stream": False}),
    ],
)
async def test_anthropic_chat_rejects_tool_stream_selector_bypasses_before_billing(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
    stream: object,
    params_enforced: dict[str, object] | None,
) -> None:
    _patch(monkeypatch)
    api_key, team, admin = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-sonnet-4-5",
        params_enforced=params_enforced,
    )

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            "stream": stream,
        },
        headers=_bearer(api_key),
    )

    assert response.status_code == HTTP_501_NOT_IMPLEMENTED
    assert FakeAnthropic.calls == 0
    assert await _team_usage(client, team, admin) == []


async def test_anthropic_responses_emulated(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-3-5-sonnet",
    )
    resp = await client.post(
        "/v1/responses", json={"model": "m", "input": "hi"}, headers=_bearer(api_key)
    )
    assert resp.status_code == HTTP_200_OK
    body = resp.json()
    assert body["object"] == "response"
    assert body["output_text"] == "hi there"


async def test_vertex_chat_translation(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client,
        provider="vertex_ai",
        values=VERTEX_VALUES,
        provider_model_id="gemini-1.5-pro",
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hi"},
            ],
        },
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    # Client built for Vertex with project/location from the credential.
    assert FakeGenaiClient.last_init["vertexai"] is True
    assert FakeGenaiClient.last_init["project"] == "p"
    assert FakeGenaiClient.last_init["location"] == "us-central1"
    # Client built with the gateway's resilience config (timeout, as HttpOptions ms).
    assert FakeGenaiClient.last_init["http_options"].timeout == int(DEFAULT_REQUEST_TIMEOUT * 1000)
    # Request: system -> system_instruction, assistant role would map to "model".
    assert FakeGenaiClient.last_kwargs["model"] == "gemini-1.5-pro"
    assert FakeGenaiClient.last_kwargs["config"]["system_instruction"] == "be brief"
    assert FakeGenaiClient.last_kwargs["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    # Response: translated back to OpenAI chat shape.
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "ciao"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 4
    assert body["usage"]["completion_tokens"] == 2
    # The per-request genai client must be closed after the call (no pool leak).
    assert FakeGenaiClient.closed is True


async def test_vertex_responses_emulated(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="vertex_ai", values=VERTEX_VALUES, provider_model_id="gemini-1.5-pro"
    )
    resp = await client.post(
        "/v1/responses", json={"model": "m", "input": "hi"}, headers=_bearer(api_key)
    )
    assert resp.status_code == HTTP_200_OK
    assert resp.json()["output_text"] == "ciao"
