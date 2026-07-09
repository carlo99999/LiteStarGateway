"""Contract: streaming chat.completions (SSE).

The shapes a streaming client depends on:
  * chunk / delta shape (`chat.completion.chunk`, `choices[].delta`);
  * a terminal chunk carrying `finish_reason`;
  * incremental tool-call deltas accumulating across chunks;
  * the wire-level `data: [DONE]` sentinel that ends the stream.
Delta shapes are asserted through the official SDK's typed stream; the `[DONE]`
sentinel — which the SDK consumes internally — is asserted on the raw wire."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from completions.conftest import _setup
from litestar.testing import AsyncTestClient
from openai import AsyncOpenAI

from .conftest import WEATHER_TOOL, _patch_upstream, raw_sse_body


async def test_text_stream_delta_shape(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_upstream(monkeypatch)
    api_key = await _setup(client)
    stream = await sdk(api_key).chat.completions.create(
        model="m", messages=[{"role": "user", "content": "hi"}], stream=True
    )
    content = ""
    finish_reason = None
    saw_chunk_object = False
    async for chunk in stream:
        assert chunk.object == "chat.completion.chunk"
        saw_chunk_object = True
        if chunk.choices:
            delta = chunk.choices[0].delta
            if delta.content:
                content += delta.content
            if chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason
    assert saw_chunk_object
    assert content == "Hi there"
    assert finish_reason == "stop"


async def test_tool_call_stream_deltas_accumulate(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_upstream(monkeypatch)
    api_key = await _setup(client)
    stream = await sdk(api_key).chat.completions.create(
        model="m",
        messages=[{"role": "user", "content": "weather in Paris?"}],
        tools=[WEATHER_TOOL],
        stream=True,
    )
    name = ""
    arguments = ""
    finish_reason = None
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        for tc in delta.tool_calls or []:
            if tc.function and tc.function.name:
                name += tc.function.name
            if tc.function and tc.function.arguments:
                arguments += tc.function.arguments
        if chunk.choices[0].finish_reason:
            finish_reason = chunk.choices[0].finish_reason
    assert name == "get_weather"
    # The fragments concatenate into one valid JSON argument object.
    assert json.loads(arguments) == {"city": "Paris"}
    assert finish_reason == "tool_calls"


async def test_stream_terminates_with_done_sentinel(
    client: AsyncTestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The wire contract: the SSE body ends with `data: [DONE]` (the SDK relies on
    # it to stop iterating). Asserted on the raw stream since the SDK hides it.
    _patch_upstream(monkeypatch)
    api_key = await _setup(client)
    body = await raw_sse_body(
        client,
        api_key,
        {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert "chat.completion.chunk" in body
    assert "data: [DONE]" in body
