"""SSE streaming across providers (chat + Responses), and its usage billing."""

from __future__ import annotations

import pytest
from litestar.status_codes import HTTP_200_OK, HTTP_404_NOT_FOUND, HTTP_501_NOT_IMPLEMENTED
from litestar.testing import AsyncTestClient

from .conftest import (
    ADMIN_EMAIL,
    ANTHROPIC_VALUES,
    DATABRICKS_VALUES,
    OPENAI_VALUES,
    VERTEX_VALUES,
    FakeAnthropic,
    FakeClient,
    FakeGenaiClient,
    _admin,
    _bearer,
    _patch,
    _setup,
    _setup_team,
    _team_usage,
)


async def test_streaming_openai_sse(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real OpenAI streaming via the SDK (mocked): stream=True passed, chunks relayed.
    _patch(monkeypatch)
    api_key = await _setup(client)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [], "stream": True},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert "text/event-stream" in resp.headers["content-type"]
    assert FakeClient.last_kwargs["stream"] is True
    assert FakeClient.last_kwargs["model"] == "gpt-4o"
    body = resp.text
    assert "chat.completion.chunk" in body
    assert "Hi" in body
    assert "data: [DONE]" in body


async def test_streaming_records_usage(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A streamed chat call must still be billed + observed (previously it wasn't).
    _patch(monkeypatch)
    admin = await _admin(client)
    cred = (
        await client.post(
            "/credentials",
            json={"name": "c", "provider": "openai", "values": OPENAI_VALUES},
            headers=_bearer(admin),
        )
    ).json()["id"]
    org = (
        await client.post("/organizations", json={"name": "Acme"}, headers=_bearer(admin))
    ).json()["id"]
    team = (
        await client.post(
            f"/organizations/{org}/teams",
            json={"name": "Core", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    await client.post(
        f"/teams/{team}/models",
        json={
            "name": "m",
            "provider": "openai",
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "gpt-4o",
            "enabled": True,
        },
        headers=_bearer(admin),
    )
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    ).json()["plaintext"]

    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [], "stream": True},
        headers=_bearer(key),
    )
    assert resp.status_code == HTTP_200_OK
    _ = resp.text  # fully consume the stream so the final usage chunk is metered
    # The gateway forces include_usage so streamed calls can be metered.
    assert FakeClient.last_kwargs["stream_options"] == {"include_usage": True}

    # A usage row now exists for the streamed call, with the streamed token counts.
    rows = (await client.get(f"/teams/{team}/usage", headers=_bearer(admin))).json()
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == 5
    assert rows[0]["completion_tokens"] == 7
    assert rows[0]["calls"] == 1
    assert rows[0]["requested_alias"] == "m"
    assert rows[0]["canonical_model_name"] == "m"
    assert rows[0]["callable_origin"] == "own"


async def test_streaming_databricks_sse(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Databricks shares the OpenAI client surface → streaming works via delegation.
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="databricks", values=DATABRICKS_VALUES, provider_model_id="my-endpoint"
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [], "stream": True},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeClient.last_kwargs["stream"] is True
    assert "data: [DONE]" in resp.text


async def test_streaming_anthropic_sse(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Anthropic Messages stream events translated to OpenAI chunks.
    _patch(monkeypatch)
    api_key = await _setup(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-3-5-sonnet",
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeAnthropic.last_kwargs["stream"] is True
    body = resp.text
    assert "chat.completion.chunk" in body
    assert "Hi" in body and "there" in body
    assert '"finish_reason": "stop"' in body
    assert "data: [DONE]" in body


async def test_streaming_vertex_sse(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Gemini stream chunks translated to OpenAI chunks.
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="vertex_ai", values=VERTEX_VALUES, provider_model_id="gemini-1.5-pro"
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeGenaiClient.last_kwargs["model"] == "gemini-1.5-pro"
    body = resp.text
    assert "chat.completion.chunk" in body
    assert "ci" in body and "ao" in body
    assert '"finish_reason": "stop"' in body
    assert "data: [DONE]" in body


async def test_streaming_responses_native_openai(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # OpenAI native Responses streaming: events passed through as typed SSE.
    _patch(monkeypatch)
    api_key = await _setup(client)
    resp = await client.post(
        "/v1/responses",
        json={"model": "m", "input": "hi", "stream": True},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert "text/event-stream" in resp.headers["content-type"]
    assert FakeClient.last_kwargs["stream"] is True
    body = resp.text
    assert "event: response.output_text.delta" in body
    assert "event: response.completed" in body
    assert "Hi" in body


async def test_streaming_responses_emulated_anthropic(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Anthropic has no native Responses → emulated over its chat stream.
    _patch(monkeypatch)
    api_key = await _setup(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-3-5-sonnet",
    )
    resp = await client.post(
        "/v1/responses",
        json={"model": "m", "input": "hi", "stream": True},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    body = resp.text
    assert "event: response.output_text.delta" in body
    assert "event: response.completed" in body
    assert "Hi" in body and "there" in body


@pytest.mark.parametrize(
    "payload,match",
    [
        ({"input": "hi", "previous_response_id": "resp_previous"}, "previous_response_id"),
        (
            {"input": [{"role": "developer", "content": "Never disclose secrets"}]},
            "developer",
        ),
    ],
)
async def test_streaming_responses_rejects_before_sse_and_provider(
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
        json={
            "model": "m",
            "stream": True,
            **payload,
        },
        headers=_bearer(api_key),
    )

    assert resp.status_code == HTTP_501_NOT_IMPLEMENTED
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["error"]["code"] == "UnsupportedOperation"
    assert match in resp.json()["error"]["message"]
    assert FakeAnthropic.last_kwargs == {}


async def test_streaming_anthropic_records_usage(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Anthropic reports stream usage on message_start/message_delta events; the
    # gateway must translate it and bill it (previously recorded as 0 tokens).
    _patch(monkeypatch)
    key, team, admin = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-3-5-sonnet",
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_bearer(key),
    )
    assert resp.status_code == HTTP_200_OK
    _ = resp.text  # fully consume the stream
    rows = await _team_usage(client, team, admin)
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == 3
    assert rows[0]["completion_tokens"] == 5
    assert rows[0]["calls"] == 1


async def test_streaming_vertex_records_usage(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Gemini reports stream usage via usage_metadata on the final chunk; the
    # gateway must translate it and bill it (previously recorded as 0 tokens).
    _patch(monkeypatch)
    key, team, admin = await _setup_team(
        client, provider="vertex_ai", values=VERTEX_VALUES, provider_model_id="gemini-1.5-pro"
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_bearer(key),
    )
    assert resp.status_code == HTTP_200_OK
    _ = resp.text  # fully consume the stream
    rows = await _team_usage(client, team, admin)
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == 4
    assert rows[0]["completion_tokens"] == 2
    assert rows[0]["calls"] == 1


async def test_responses_native_records_usage(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Responses-API usage uses input/output_tokens keys — billing must map them
    # (previously only prompt/completion_tokens were read → billed as 0).
    _patch(monkeypatch)
    key, team, admin = await _setup_team(client)
    resp = await client.post(
        "/v1/responses", json={"model": "m", "input": "hi"}, headers=_bearer(key)
    )
    assert resp.status_code == HTTP_200_OK
    rows = await _team_usage(client, team, admin)
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == 2
    assert rows[0]["completion_tokens"] == 3
    assert rows[0]["calls"] == 1


async def test_streaming_responses_native_records_usage(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Native Responses streaming carries usage on the response.completed event.
    _patch(monkeypatch)
    key, team, admin = await _setup_team(client)
    resp = await client.post(
        "/v1/responses",
        json={"model": "m", "input": "hi", "stream": True},
        headers=_bearer(key),
    )
    assert resp.status_code == HTTP_200_OK
    _ = resp.text  # fully consume the stream
    rows = await _team_usage(client, team, admin)
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == 2
    assert rows[0]["completion_tokens"] == 3
    assert rows[0]["calls"] == 1


async def test_responses_emulated_records_usage(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Emulated (non-streaming) Responses over Anthropic chat must bill the
    # underlying chat usage (input 3 / output 5 from the fake).
    _patch(monkeypatch)
    key, team, admin = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-3-5-sonnet",
    )
    resp = await client.post(
        "/v1/responses", json={"model": "m", "input": "hi"}, headers=_bearer(key)
    )
    assert resp.status_code == HTTP_200_OK
    rows = await _team_usage(client, team, admin)
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == 3
    assert rows[0]["completion_tokens"] == 5
    assert rows[0]["calls"] == 1


async def test_streaming_responses_emulated_records_usage(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Emulated Responses streaming must propagate the inner chat stream's usage
    # into response.completed and bill it (previously dropped entirely).
    _patch(monkeypatch)
    key, team, admin = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-3-5-sonnet",
    )
    resp = await client.post(
        "/v1/responses",
        json={"model": "m", "input": "hi", "stream": True},
        headers=_bearer(key),
    )
    assert resp.status_code == HTTP_200_OK
    _ = resp.text  # fully consume the stream
    rows = await _team_usage(client, team, admin)
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == 3
    assert rows[0]["completion_tokens"] == 5
    assert rows[0]["calls"] == 1


async def test_streaming_unknown_model_404_before_stream(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Resolution happens before the SSE starts → a clean 404, not a broken stream.
    _patch(monkeypatch)
    api_key = await _setup(client)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "nope", "messages": [], "stream": True},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_404_NOT_FOUND
