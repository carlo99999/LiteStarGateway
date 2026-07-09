"""Contract: the non-streaming chat.completions response envelope.

Any compliant OpenAI client depends on `choices[].message`, `finish_reason`
values and a `usage` block with prompt/completion/total tokens. Asserted here by
parsing the response through the official SDK's typed model (a shape mismatch
would raise before these assertions), for an OpenAI-backed model. Azure and
Databricks share the same OpenAI-compatible surface and envelope."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from completions.conftest import AZURE_VALUES, DATABRICKS_VALUES, _setup
from litestar.testing import AsyncTestClient
from openai import AsyncOpenAI

from .conftest import _patch_upstream


async def test_response_envelope_shape(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_upstream(monkeypatch)
    api_key = await _setup(client)
    completion = await sdk(api_key).chat.completions.create(
        model="m", messages=[{"role": "user", "content": "hi"}]
    )
    # Envelope identity.
    assert completion.object == "chat.completion"
    assert completion.choices, "at least one choice"
    choice = completion.choices[0]
    assert choice.index == 0
    # The message the client reads.
    assert choice.message.role == "assistant"
    assert choice.message.content == "It is sunny in Paris."
    # A terminal finish_reason from the OpenAI enum.
    assert choice.finish_reason == "stop"
    # Usage the client bills/telemeters on.
    assert completion.usage is not None
    assert completion.usage.prompt_tokens == 5
    assert completion.usage.completion_tokens == 7
    assert completion.usage.total_tokens == 12


@pytest.mark.parametrize(
    "provider,values,provider_model_id",
    [
        ("azure_openai", AZURE_VALUES, "gpt-4o"),
        ("databricks", DATABRICKS_VALUES, "my-endpoint"),
    ],
)
async def test_response_envelope_shape_other_openai_backends(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    values: dict,
    provider_model_id: str,
) -> None:
    # The contract holds identically for the other OpenAI-compatible backends.
    _patch_upstream(monkeypatch)
    api_key = await _setup(
        client, provider=provider, values=values, provider_model_id=provider_model_id
    )
    completion = await sdk(api_key).chat.completions.create(
        model="m", messages=[{"role": "user", "content": "hi"}]
    )
    assert completion.object == "chat.completion"
    assert completion.choices[0].message.content == "It is sunny in Paris."
    assert completion.choices[0].finish_reason == "stop"
    assert completion.usage is not None
    assert completion.usage.total_tokens == 12
