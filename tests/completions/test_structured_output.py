"""Cross-provider structured outputs (response_format / text.format): chat,
streaming, and the Responses API. See docs/structured-outputs.md."""

from __future__ import annotations

import json

import pytest
from conftest import (
    ANTHROPIC_VALUES,
    VERTEX_VALUES,
    FakeAnthropic,
    FakeClient,
    FakeGenaiClient,
    _bearer,
    _patch,
    _setup,
)
from litestar.status_codes import HTTP_200_OK
from litestar.testing import AsyncTestClient

from litestar_gateway.infrastructure.llm import anthropic_adapter

_JSON_SCHEMA_RF = {
    "type": "json_schema",
    "json_schema": {
        "name": "answer",
        "schema": {"type": "object", "properties": {"answer": {"type": "integer"}}},
    },
}


async def test_openai_response_format_passes_through(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # OpenAI/Azure accept response_format natively — it must reach the SDK verbatim.
    _patch(monkeypatch)
    api_key = await _setup(client)
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            **{"response_format": _JSON_SCHEMA_RF},
        },
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeClient.last_kwargs["response_format"] == _JSON_SCHEMA_RF


async def test_anthropic_structured_output_forces_a_tool(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Anthropic has no native JSON mode: a json_schema request must be translated
    # into a forced tool, and the tool's input surfaced as JSON message content.
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="anthropic", values=ANTHROPIC_VALUES, provider_model_id="claude-3-5-sonnet"
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": _JSON_SCHEMA_RF,
        },
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    tools = FakeAnthropic.last_kwargs["tools"]
    assert tools[0]["name"] == "answer"
    assert tools[0]["input_schema"] == _JSON_SCHEMA_RF["json_schema"]["schema"]
    assert FakeAnthropic.last_kwargs["tool_choice"] == {"type": "tool", "name": "answer"}
    # The forced tool's input is surfaced as JSON in message.content.
    choice = resp.json()["choices"][0]
    assert json.loads(choice["message"]["content"]) == {"answer": 42}
    # A structured completion looks like a normal one to the client, not a tool
    # call — finish_reason must be "stop", not "tool_calls".
    assert choice["finish_reason"] == "stop"


async def test_anthropic_json_object_nudges_via_system(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A bare json_object request (no schema) can't force a tool; nudge via system.
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="anthropic", values=ANTHROPIC_VALUES, provider_model_id="claude-3-5-sonnet"
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_object"},
        },
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert "tools" not in FakeAnthropic.last_kwargs  # no schema -> no forced tool
    assert "JSON" in (FakeAnthropic.last_kwargs.get("system") or "")


async def test_gemini_structured_output_sets_response_schema(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Gemini structured output = response_mime_type + response_schema in config.
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="vertex_ai", values=VERTEX_VALUES, provider_model_id="gemini-1.5-pro"
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": _JSON_SCHEMA_RF,
        },
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    config = FakeGenaiClient.last_kwargs["config"]
    assert config["response_mime_type"] == "application/json"
    assert config["response_schema"] == _JSON_SCHEMA_RF["json_schema"]["schema"]


def _sse_events(body: str) -> list[dict]:
    """Parse the JSON payloads out of an SSE response body."""
    events = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :].strip()
        if payload != "[DONE]":
            events.append(json.loads(payload))
    return events


def _sse_content(body: str) -> str:
    """Reassemble the streamed assistant text from a chat SSE response body."""
    out = ""
    for event in _sse_events(body):
        for choice in event.get("choices", []):
            out += (choice.get("delta") or {}).get("content") or ""
    return out


def _sse_responses_text(body: str) -> str:
    """Reassemble streamed text from a Responses SSE body (output_text deltas)."""
    return "".join(
        ev.get("delta") or ""
        for ev in _sse_events(body)
        if ev.get("type") == "response.output_text.delta"
    )


def test_anthropic_input_json_delta_maps_to_content() -> None:
    # The forced tool streams its JSON via input_json_delta, not text_delta.
    delta, finish = anthropic_adapter.anthropic_event_to_delta(
        {
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": '{"a":1}'},
        }
    )
    assert delta == {"content": '{"a":1}'}
    assert finish is None


async def test_anthropic_structured_output_streams_json(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="anthropic", values=ANTHROPIC_VALUES, provider_model_id="claude-3-5-sonnet"
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "response_format": _JSON_SCHEMA_RF,
        },
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeAnthropic.last_kwargs["tool_choice"] == {"type": "tool", "name": "answer"}
    # The partial-JSON tool deltas reassemble into the structured result.
    body = resp.text
    assert json.loads(_sse_content(body)) == {"answer": 42}
    # Streamed finish_reason is "stop" too (not "tool_calls").
    finishes = [
        choice.get("finish_reason")
        for ev in _sse_events(body)
        for choice in ev.get("choices", [])
        if choice.get("finish_reason")
    ]
    assert finishes == ["stop"]


async def test_openai_structured_output_streaming_passes_through(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(client)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [], "stream": True, "response_format": _JSON_SCHEMA_RF},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeClient.last_kwargs["stream"] is True
    assert FakeClient.last_kwargs["response_format"] == _JSON_SCHEMA_RF


async def test_gemini_structured_output_streaming_sets_schema(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="vertex_ai", values=VERTEX_VALUES, provider_model_id="gemini-1.5-pro"
    )
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "response_format": _JSON_SCHEMA_RF,
        },
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    config = FakeGenaiClient.last_kwargs["config"]
    assert config["response_mime_type"] == "application/json"
    assert config["response_schema"] == _JSON_SCHEMA_RF["json_schema"]["schema"]


_TEXT_FORMAT = {
    "type": "json_schema",
    "name": "answer",
    "schema": {"type": "object", "properties": {"answer": {"type": "integer"}}},
}


def test_responses_text_format_maps_to_response_format() -> None:
    from litestar_gateway.infrastructure.llm.responses_emulation import to_chat_completions

    chat = to_chat_completions({"input": "hi", "text": {"format": _TEXT_FORMAT}})
    assert chat["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "answer", "schema": _TEXT_FORMAT["schema"]},
    }


async def test_responses_native_passes_text_format(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # OpenAI has a native Responses API — text.format must reach the SDK verbatim.
    _patch(monkeypatch)
    api_key = await _setup(client)
    resp = await client.post(
        "/v1/responses",
        json={"model": "m", "input": "hi", "text": {"format": _TEXT_FORMAT}},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeClient.last_kwargs["text"] == {"format": _TEXT_FORMAT}


async def test_responses_emulated_structured_output(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Emulated Responses (Anthropic): text.format -> chat response_format ->
    # forced tool -> the JSON is surfaced as the Responses output_text.
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="anthropic", values=ANTHROPIC_VALUES, provider_model_id="claude-3-5-sonnet"
    )
    resp = await client.post(
        "/v1/responses",
        json={"model": "m", "input": "hi", "text": {"format": _TEXT_FORMAT}},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeAnthropic.last_kwargs["tool_choice"] == {"type": "tool", "name": "answer"}
    assert json.loads(resp.json()["output_text"]) == {"answer": 42}


async def test_responses_emulated_structured_output_streaming(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The streaming emulation shares to_chat_completions, so text.format still
    # forces the tool on the streamed path.
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="anthropic", values=ANTHROPIC_VALUES, provider_model_id="claude-3-5-sonnet"
    )
    resp = await client.post(
        "/v1/responses",
        json={"model": "m", "input": "hi", "stream": True, "text": {"format": _TEXT_FORMAT}},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeAnthropic.last_kwargs["tool_choice"] == {"type": "tool", "name": "answer"}
    # The tool's partial-JSON deltas stream through as output_text and reassemble.
    assert json.loads(_sse_responses_text(resp.text)) == {"answer": 42}
