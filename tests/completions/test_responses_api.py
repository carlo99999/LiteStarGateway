"""Non-streaming Responses API (`/v1/responses`), per provider."""

from __future__ import annotations

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_501_NOT_IMPLEMENTED
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
)


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


async def test_databricks_responses_rejects_unsupported_fields_before_provider(
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
            "tools": [{"type": "function", "name": "weather", "parameters": {}}],
        },
        headers=_bearer(api_key),
    )

    assert resp.status_code == HTTP_501_NOT_IMPLEMENTED
    assert resp.json() == {
        "error": {
            "message": (
                "Provider 'databricks' cannot emulate Responses field(s): tools; "
                "use a native Responses provider or remove the unsupported fields"
            ),
            "type": "server_error",
            "code": "UnsupportedOperation",
        }
    }
    assert FakeClient.last_kwargs == {}


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
