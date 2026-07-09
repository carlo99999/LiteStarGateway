"""Native Anthropic Messages wire-contract conformance harness (Plan 02, native
extension of the OpenAI contract suite).

The gateway is compatible with *any* Anthropic-native client (the `anthropic`
SDK, Pydantic AI's `AnthropicModel`, …) iff `POST /v1/messages` speaks the
Anthropic Messages wire protocol faithfully — native passthrough, no OpenAI
translation. This suite asserts that **contract** (response `content` /
`stop_reason` / `role` / `usage` shape, the tool_use → tool_result round-trip,
and the streaming event types) and drives it with the official `anthropic`
Python SDK as an end-to-end **canary**, never a per-framework matrix. There is
zero per-framework code here.

Wiring: the real SDK talks to the in-process Litestar app over
`httpx.ASGITransport(app=<app>)` — no server, no socket, no live API key. The
SDK authenticates with `api_key=<gateway team key>`, which it sends as the
`x-api-key` header the gateway's shared middleware accepts. The upstream
provider client the *gateway* calls is faked (the `Fake*` pattern from
`tests/completions/`), so every response is deterministic and offline.

Two distinct "anthropic" layers, do not conflate them:
  * the SDK **under test** (real `anthropic`) → the client any framework uses;
  * the upstream `AsyncAnthropic` **inside the adapter** (faked here) → provider.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any, cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types import Message, MessageParam, TextBlock, ToolParam
from completions.conftest import ANTHROPIC_VALUES, _bearer, _setup_team
from litestar.testing import AsyncTestClient

# Re-export the completions harness `client` fixture (same app, same team/model/
# key setup). `tests/` is on sys.path during collection, so `completions` and
# `conformance` import as top-level packages (mirrors tests/native/conftest.py).
from conformance.conftest import client as client  # noqa: F401  (fixture re-export)
from litestar_gateway.infrastructure.llm import anthropic_adapter

# The team model behind the native endpoint must be Anthropic-backed.
_MODEL_ID = "claude-3-5-sonnet-20241022"

# A well-formed Anthropic tool spec (the shape a compliant native client sends).
WEATHER_TOOL: ToolParam = {
    "name": "get_weather",
    "description": "Get the current weather for a city.",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}


class _Result:
    """Minimal stand-in for the upstream SDK response: only `model_dump` is used
    by the adapter under test."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def model_dump(self) -> dict[str, Any]:
        return self._data


class _FakeStream:
    """Async-iterable of event objects, like the upstream SDK's AsyncMessageStream
    (each event exposes `model_dump()`, which the adapter relays verbatim)."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def __aiter__(self) -> AsyncIterator[_Result]:
        for event in self._events:
            yield _Result(event)


def _has_tool_result(messages: list[Any]) -> bool:
    """A tool-result round-trip is in progress once the conversation carries a
    `tool_result` content block (the Anthropic shape for answering a tool_use)."""
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_result" for block in content
        ):
            return True
    return False


def _tool_use_message(model: str | None) -> dict[str, Any]:
    return {
        "id": "msg-tool",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [
            {"type": "text", "text": "Let me check the weather."},
            {
                "type": "tool_use",
                "id": "toolu_01",
                "name": "get_weather",
                "input": {"city": "Paris"},
            },
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 15},
    }


def _final_text_message(model: str | None) -> dict[str, Any]:
    return {
        "id": "msg-final",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "It is sunny in Paris."}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 20, "output_tokens": 8},
    }


def _text_stream_events(model: str | None) -> list[dict[str, Any]]:
    """A well-formed Anthropic Messages stream: the exact event sequence the real
    SDK's stream accumulator expects (message_start with a full Message,
    content_block_start/delta/stop, message_delta with the final usage, stop)."""
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg-s",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": " world"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 7},
        },
        {"type": "message_stop"},
    ]


class FakeUpstream:
    """Fake upstream Anthropic SDK client the *gateway* calls.

    Echoes native Anthropic-shaped responses. With `tools` present it emits a
    `tool_use` message — until the conversation already carries a `tool_result`
    (the round-trip), when it emits a final text answer instead. Shares the
    capture pattern of `tests/completions`' `FakeAnthropic`."""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        self.messages = self

    async def close(self) -> None:  # AsyncAnthropic.close is a coroutine
        return None

    async def create(self, **kwargs: Any) -> Any:
        FakeUpstream.last_kwargs = kwargs
        model = kwargs.get("model")
        messages = kwargs.get("messages") or []
        wants_tools = kwargs.get("tools") is not None
        answering_tool = _has_tool_result(messages)
        if kwargs.get("stream"):
            return _FakeStream(_text_stream_events(model))
        if wants_tools and not answering_tool:
            return _Result(_tool_use_message(model))
        return _Result(_final_text_message(model))


def _patch_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the Anthropic adapter's upstream SDK client at the fake."""
    monkeypatch.setattr(anthropic_adapter, "AsyncAnthropic", FakeUpstream)
    FakeUpstream.last_kwargs = {}


def _anthropic_sdk(test_client: AsyncTestClient, api_key: str) -> AsyncAnthropic:
    """The official `anthropic` SDK, wired to the in-process app over ASGI
    transport. `base_url` is the gateway root so the SDK targets `/v1/messages`;
    `api_key` is sent as `x-api-key` (the gateway accepts it). `max_retries=0`
    keeps error cases a single deterministic round-trip."""
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_client.app),
        base_url="http://gateway",
    )
    return AsyncAnthropic(
        api_key=api_key,
        base_url="http://gateway",
        http_client=http_client,
        max_retries=0,
    )


@pytest.fixture
async def sdk(
    client: AsyncTestClient,
) -> AsyncIterator[Callable[[str], AsyncAnthropic]]:
    """Factory: `sdk(api_key)` -> an `anthropic` client bound to the running app."""
    created: list[AsyncAnthropic] = []

    def factory(api_key: str) -> AsyncAnthropic:
        sdk_client = _anthropic_sdk(client, api_key)
        created.append(sdk_client)
        return sdk_client

    yield factory
    for sdk_client in created:
        await sdk_client.close()


async def _anthropic_key(client: AsyncTestClient, **overrides: Any) -> tuple[str, str, str]:
    return await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id=_MODEL_ID,
        model_type="chat",
        **overrides,
    )


def _round_trip_messages(first: Message, tool_use_id: str) -> list[MessageParam]:
    """The second turn of a tool loop: the original question, the assistant's
    `tool_use` turn echoed back, and the client's `tool_result`. `cast` because
    the freeform block dicts don't narrow to Anthropic's block-param unions."""
    return cast(
        "list[MessageParam]",
        [
            {"role": "user", "content": "What is the weather in Paris?"},
            {"role": "assistant", "content": [block.model_dump() for block in first.content]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id, "content": "18C and sunny"}
                ],
            },
        ],
    )


async def test_response_wire_shape(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncAnthropic],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The native contract: a `Message` with role/stop_reason/typed content blocks
    # and Anthropic-native `usage` (input_tokens/output_tokens) — NOT the OpenAI
    # choices/finish_reason/prompt_tokens shape.
    _patch_upstream(monkeypatch)
    key, _, _ = await _anthropic_key(client)
    message = await sdk(key).messages.create(
        model="m",
        max_tokens=64,
        messages=[{"role": "user", "content": "hello"}],
    )
    assert message.role == "assistant"
    assert message.type == "message"
    assert message.stop_reason == "end_turn"
    assert message.content[0].type == "text"
    assert message.content[0].text == "It is sunny in Paris."
    assert message.usage.input_tokens == 20
    assert message.usage.output_tokens == 8
    # The team alias was resolved to the upstream provider id (passthrough, not
    # translation): the upstream client saw the real model, not the alias.
    assert FakeUpstream.last_kwargs["model"] == _MODEL_ID


async def test_tool_use_round_trip(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncAnthropic],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The full native tool loop: request with `tools` -> `tool_use` block ->
    # client returns a `tool_result` -> final assistant text. Proves tools pass
    # through untranslated and a tool-result turn is accepted.
    _patch_upstream(monkeypatch)
    key, _, _ = await _anthropic_key(client)
    anthropic_client = sdk(key)

    # Turn 1: the model asks for the tool.
    first = await anthropic_client.messages.create(
        model="m",
        max_tokens=64,
        tools=[WEATHER_TOOL],
        messages=[{"role": "user", "content": "What is the weather in Paris?"}],
    )
    assert first.stop_reason == "tool_use"
    tool_use = next(block for block in first.content if block.type == "tool_use")
    assert tool_use.name == "get_weather"
    assert tool_use.input == {"city": "Paris"}
    # tools were forwarded to the provider unchanged (not dropped/translated).
    assert FakeUpstream.last_kwargs["tools"][0]["name"] == "get_weather"

    # Turn 2: feed the assistant turn + the tool's result back (the round-trip).
    second = await anthropic_client.messages.create(
        model="m",
        max_tokens=64,
        tools=[WEATHER_TOOL],
        messages=_round_trip_messages(first, tool_use.id),
    )
    assert second.stop_reason == "end_turn"
    answer = second.content[0]
    assert isinstance(answer, TextBlock)
    assert answer.text == "It is sunny in Paris."
    # The gateway relayed the tool_result turn upstream (the round-trip reached it).
    assert _has_tool_result(FakeUpstream.last_kwargs["messages"])


async def test_streaming_event_types(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncAnthropic],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The raw stream relays native Anthropic event types in order (the SDK parses
    # them from the gateway's named SSE events), never OpenAI chunks.
    _patch_upstream(monkeypatch)
    key, _, _ = await _anthropic_key(client)
    stream = await sdk(key).messages.create(
        model="m",
        max_tokens=64,
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )
    types = [event.type async for event in stream]
    assert types == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]


async def test_streaming_high_level_accumulates(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncAnthropic],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The SDK's high-level `messages.stream(...)` helper accumulates the relayed
    # SSE into text and a final `Message` with native usage — proof the gateway's
    # Anthropic-format SSE is well-formed enough for the SDK's stream accumulator.
    _patch_upstream(monkeypatch)
    key, _, _ = await _anthropic_key(client)
    text_parts: list[str] = []
    async with sdk(key).messages.stream(
        model="m",
        max_tokens=64,
        messages=[{"role": "user", "content": "hi"}],
    ) as stream:
        async for text in stream.text_stream:
            text_parts.append(text)
        final = await stream.get_final_message()
    assert "".join(text_parts) == "Hello world"
    assert final.stop_reason == "end_turn"
    assert final.usage.input_tokens == 5
    assert final.usage.output_tokens == 7


async def test_tool_round_trip_bills_native_tokens(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncAnthropic],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Governance holds on the native path: each native call is metered once on the
    # native token counts. The two-turn tool loop records two calls, summing the
    # native input_tokens (10 + 20) and output_tokens (15 + 8) at the model's rate.
    _patch_upstream(monkeypatch)
    key, team, admin = await _anthropic_key(
        client, input_cost_per_token=0.01, output_cost_per_token=0.01
    )
    anthropic_client = sdk(key)
    first = await anthropic_client.messages.create(
        model="m",
        max_tokens=64,
        tools=[WEATHER_TOOL],
        messages=[{"role": "user", "content": "What is the weather in Paris?"}],
    )
    tool_use = next(block for block in first.content if block.type == "tool_use")
    await anthropic_client.messages.create(
        model="m",
        max_tokens=64,
        tools=[WEATHER_TOOL],
        messages=_round_trip_messages(first, tool_use.id),
    )
    usage = (await client.get(f"/teams/{team}/usage", headers=_bearer(admin))).json()
    assert len(usage) == 1
    (row,) = usage
    assert row["calls"] == 2
    assert row["prompt_tokens"] == 30  # native input_tokens: 10 + 20
    assert row["completion_tokens"] == 23  # native output_tokens: 15 + 8
    assert row["cost"] == pytest.approx((30 + 23) * 0.01)
