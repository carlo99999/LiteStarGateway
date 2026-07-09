"""OpenAI Chat Completions wire-contract conformance harness (Plan 02, phase 1).

The gateway is compatible with *any* agent framework iff it speaks the OpenAI
Chat Completions wire protocol faithfully. This suite asserts that **contract**
— response/tool/stream/error shapes — and drives it with the official `openai`
Python SDK as an end-to-end **canary** (proof the wire holds in practice), never
as a per-framework matrix. There is zero per-framework code here.

Wiring: the real SDK talks to the in-process Litestar app over
`httpx.ASGITransport(app=<app>)` — no server, no socket, no live API key. The
upstream provider SDK the *gateway* calls is faked (the same `Fake*` pattern as
`tests/completions/`), so every response is deterministic and offline.

Two distinct "openai" layers, do not conflate them:
  * the SDK **under test** (real `openai`) → the client any framework would use;
  * the upstream `AsyncOpenAI` **inside the adapter** (faked here) → the provider.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from completions.conftest import FakeAnthropic, _bearer

# Re-export the completions harness verbatim: same app, same credential/team/
# model/key setup. `tests/` is on sys.path during collection, so `completions`
# imports as a top-level package (mirrors tests/native/conftest.py). The
# redundant `client as client` alias marks the fixture as an intentional
# re-export (so it registers for this directory's tests) without ruff flagging
# it against the `sdk` fixture's `client` parameter.
from completions.conftest import client as client  # noqa: F401  (fixture re-export)
from litestar.testing import AsyncTestClient
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionToolParam

from litestar_gateway.infrastructure.llm import (
    anthropic_adapter,
    azure_adapter,
    openai_adapter,
)

# A well-formed OpenAI tool spec (the shape a compliant client sends).
WEATHER_TOOL: ChatCompletionToolParam = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


class _Result:
    """Minimal stand-in for an SDK response object: only `model_dump` is used by
    the adapter under test."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def model_dump(self) -> dict[str, Any]:
        return self._data


class _FakeStream:
    """Async-iterable of chunk objects, like the upstream SDK's AsyncStream."""

    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[_Result]:
        for chunk in self._chunks:
            yield _Result(chunk)


def _has_tool_result(messages: list[Any]) -> bool:
    """A tool-result round-trip is in progress once the conversation carries a
    `tool` message answering a prior assistant `tool_calls`."""
    return any(isinstance(m, dict) and m.get("role") == "tool" for m in messages)


def _tool_call_response(model: str | None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-tool",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
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
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 9, "completion_tokens": 12, "total_tokens": 21},
    }


def _text_response(model: str | None, content: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
    }


def _text_stream_chunks(model: str | None) -> list[dict[str, Any]]:
    base = {"id": "chatcmpl-s", "object": "chat.completion.chunk", "model": model}
    return [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {**base, "choices": [{"index": 0, "delta": {"content": "Hi"}}]},
        {**base, "choices": [{"index": 0, "delta": {"content": " there"}}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {**base, "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7}},
    ]


def _tool_stream_chunks(model: str | None) -> list[dict[str, Any]]:
    """A tool call streamed as incremental deltas: the id/name arrive first, then
    the arguments accumulate across chunks, then a terminal `tool_calls` stop."""
    base = {"id": "chatcmpl-s", "object": "chat.completion.chunk", "model": model}
    return [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_abc123",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": ""},
                            }
                        ]
                    },
                }
            ],
        },
        {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]},
                }
            ],
        },
        {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' "Paris"}'}}]},
                }
            ],
        },
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {**base, "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 12}},
    ]


class FakeUpstream:
    """Fake upstream OpenAI-compatible SDK client (OpenAI/Azure/Databricks).

    Echoes OpenAI-shaped responses. When the request carries `tools`, it emits a
    `tool_calls` response — until the conversation already contains a `tool`
    message (the round-trip), when it emits a final text answer instead. Shares
    the capture pattern of `tests/completions`' `FakeClient`."""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        self.chat = SimpleNamespace(completions=self)

    async def close(self) -> None:  # AsyncOpenAI.close is a coroutine
        return None

    async def create(self, **kwargs: Any) -> Any:
        FakeUpstream.last_kwargs = kwargs
        model = kwargs.get("model")
        messages = kwargs.get("messages") or []
        wants_tools = kwargs.get("tools") is not None
        answering_tool = _has_tool_result(messages)
        if kwargs.get("stream"):
            chunks = (
                _tool_stream_chunks(model)
                if wants_tools and not answering_tool
                else _text_stream_chunks(model)
            )
            return _FakeStream(chunks)
        if wants_tools and not answering_tool:
            return _Result(_tool_call_response(model))
        return _Result(_text_response(model, "It is sunny in Paris."))


def _patch_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the adapters' upstream SDK clients at the fake. OpenAI and Azure
    share the OpenAI-compatible surface (`FakeUpstream`); Anthropic is stubbed so
    the H23 guard's rejection is proven without any accidental network call (the
    guard fires before the client is even built)."""
    monkeypatch.setattr(openai_adapter, "AsyncOpenAI", FakeUpstream)
    monkeypatch.setattr(azure_adapter, "AsyncAzureOpenAI", FakeUpstream)
    monkeypatch.setattr(anthropic_adapter, "AsyncAnthropic", FakeAnthropic)
    FakeUpstream.last_kwargs = {}


def _sdk_for(test_client: AsyncTestClient, api_key: str) -> AsyncOpenAI:
    """The official `openai` SDK, wired to the in-process app over ASGI transport.

    `max_retries=0` keeps error cases a single deterministic round-trip (the SDK
    would otherwise retry 5xx, and the gateway maps 501 -> InternalServerError)."""
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_client.app),
        base_url="http://gateway",
    )
    return AsyncOpenAI(
        api_key=api_key,
        base_url="http://gateway/v1",
        http_client=http_client,
        max_retries=0,
    )


@pytest.fixture
async def sdk(
    client: AsyncTestClient,
) -> AsyncIterator[Callable[[str], AsyncOpenAI]]:
    """Factory: `sdk(api_key)` -> an `openai` client bound to the running app."""
    created: list[AsyncOpenAI] = []

    def factory(api_key: str) -> AsyncOpenAI:
        sdk_client = _sdk_for(client, api_key)
        created.append(sdk_client)
        return sdk_client

    yield factory
    for sdk_client in created:
        await sdk_client.close()


async def raw_sse_body(test_client: AsyncTestClient, api_key: str, payload: dict[str, Any]) -> str:
    """Post a streaming chat request straight to the app (bypassing the SDK) and
    return the raw SSE text, so the wire-level `data: [DONE]` sentinel — which the
    SDK abstracts away — can be asserted directly."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_client.app),
        base_url="http://gateway",
    ) as raw:
        resp = await raw.post("/v1/chat/completions", json=payload, headers=_bearer(api_key))
        return resp.text
