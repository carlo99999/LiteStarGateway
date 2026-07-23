"""Contract: tool / function calling over chat.completions.

The shapes a tool-using client depends on:
  * `tools` / `tool_choice` are forwarded to the provider (honored, not dropped);
  * the response carries a well-formed `tool_calls` with
    `finish_reason == "tool_calls"`;
  * a full tool-result round-trip completes (assistant tool_call -> tool message
    -> final assistant answer).
Driven with the official SDK against an OpenAI-backed model."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import cast

import pytest
from completions.conftest import ANTHROPIC_VALUES, FakeAnthropic, _setup
from litestar.testing import AsyncTestClient
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from .conftest import WEATHER_TOOL, FakeUpstream, _patch_upstream


async def test_tools_and_tool_choice_are_forwarded(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_upstream(monkeypatch)
    api_key = await _setup(client)
    await sdk(api_key).chat.completions.create(
        model="m",
        messages=[{"role": "user", "content": "weather in Paris?"}],
        tools=[WEATHER_TOOL],
        tool_choice="auto",
    )
    # The gateway relayed tools/tool_choice to the upstream provider unchanged.
    assert FakeUpstream.last_kwargs["tool_choice"] == "auto"
    assert FakeUpstream.last_kwargs["tools"][0]["function"]["name"] == "get_weather"


async def test_tool_call_response_shape(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_upstream(monkeypatch)
    api_key = await _setup(client)
    completion = await sdk(api_key).chat.completions.create(
        model="m",
        messages=[{"role": "user", "content": "weather in Paris?"}],
        tools=[WEATHER_TOOL],
    )
    choice = completion.choices[0]
    assert choice.finish_reason == "tool_calls"
    assert choice.message.tool_calls is not None
    call = choice.message.tool_calls[0]
    assert call.id
    assert call.type == "function"
    assert call.function.name == "get_weather"
    # arguments is a JSON *string* the client parses.
    assert json.loads(call.function.arguments) == {"city": "Paris"}


async def test_tool_result_round_trip(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_upstream(monkeypatch)
    api_key = await _setup(client)
    openai_client = sdk(api_key)
    messages: list[ChatCompletionMessageParam] = [{"role": "user", "content": "weather in Paris?"}]

    # Turn 1: the model asks for the tool.
    first = await openai_client.chat.completions.create(
        model="m", messages=messages, tools=[WEATHER_TOOL]
    )
    assistant = first.choices[0].message
    assert first.choices[0].finish_reason == "tool_calls"
    assert assistant.tool_calls is not None
    tool_call = assistant.tool_calls[0]

    # Feed the assistant turn + the tool's result back (the round-trip).
    messages.append(cast(ChatCompletionMessageParam, assistant.model_dump(exclude_none=True)))
    messages.append(
        {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": "18C and sunny",
        }
    )

    # Turn 2: with a tool result in context, the model answers in natural language.
    second = await openai_client.chat.completions.create(
        model="m", messages=messages, tools=[WEATHER_TOOL]
    )
    assert second.choices[0].finish_reason == "stop"
    assert second.choices[0].message.content == "It is sunny in Paris."


async def test_anthropic_tool_result_round_trip_uses_the_same_openai_sdk_contract(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_upstream(monkeypatch)
    api_key = await _setup(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-sonnet-4-5",
    )
    openai_client = sdk(api_key)
    messages: list[ChatCompletionMessageParam] = [{"role": "user", "content": "weather in Paris?"}]

    first = await openai_client.chat.completions.create(
        model="m",
        messages=messages,
        tools=[WEATHER_TOOL],
        tool_choice="required",
        parallel_tool_calls=False,
    )

    assistant = first.choices[0].message
    assert first.choices[0].finish_reason == "tool_calls"
    assert assistant.content == "I'll check."
    assert assistant.tool_calls is not None
    tool_call = assistant.tool_calls[0]
    assert tool_call.id == "toolu-weather"
    assert tool_call.type == "function"
    assert json.loads(tool_call.function.arguments) == {"city": "Paris"}
    assert FakeAnthropic.last_kwargs["tool_choice"] == {
        "type": "any",
        "disable_parallel_tool_use": True,
    }

    messages.append(cast(ChatCompletionMessageParam, assistant.model_dump(exclude_none=True)))
    messages.append(
        {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": "18C and sunny",
        }
    )
    second = await openai_client.chat.completions.create(
        model="m",
        messages=messages,
        tools=[WEATHER_TOOL],
    )

    assert second.choices[0].finish_reason == "stop"
    assert second.choices[0].message.content == "hi there"
    assert FakeAnthropic.last_kwargs["messages"][-1] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu-weather",
                "content": "18C and sunny",
            }
        ],
    }
