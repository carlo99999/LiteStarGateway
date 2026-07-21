"""Contract: structured outputs over chat.completions (`response_format`).

An agent framework requesting a JSON schema must have `response_format` reach
the provider unchanged (not silently dropped by the allowlist) and get back a
parseable completion. Driven with the official SDK against an OpenAI-backed
model, where structured output is native."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from completions.conftest import FakeClient, _patch, _setup
from litestar.testing import AsyncTestClient
from openai import AsyncOpenAI
from openai.types.chat.completion_create_params import ResponseFormat

_JSON_SCHEMA_RF: ResponseFormat = {
    "type": "json_schema",
    "json_schema": {
        "name": "city",
        "schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
}


async def test_response_format_reaches_the_provider(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(client)
    completion = await sdk(api_key).chat.completions.create(
        model="m",
        messages=[{"role": "user", "content": "the capital of France as JSON"}],
        response_format=_JSON_SCHEMA_RF,
    )
    # The client gets a well-formed completion…
    assert completion.choices[0].message.content is not None
    # …and the schema was forwarded verbatim to the provider (not dropped).
    assert FakeClient.last_kwargs["response_format"] == _JSON_SCHEMA_RF


async def test_plain_json_object_mode_is_forwarded(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(client)
    json_object: ResponseFormat = {"type": "json_object"}
    await sdk(api_key).chat.completions.create(
        model="m",
        messages=[{"role": "user", "content": "reply in json"}],
        response_format=json_object,
    )
    assert FakeClient.last_kwargs["response_format"] == {"type": "json_object"}
