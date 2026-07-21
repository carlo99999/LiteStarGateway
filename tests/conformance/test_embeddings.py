"""Contract: embeddings over /v1/embeddings (the RAG path agent frameworks use).

Driven with the official `openai` SDK against an embeddings-typed team model, so
a LangChain/LlamaIndex embedding client works unchanged against the gateway."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from completions.conftest import FakeClient, _patch, _setup
from litestar.testing import AsyncTestClient
from openai import AsyncOpenAI


async def test_embeddings_round_trip(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client, model_type="embeddings", provider_model_id="text-embedding-3-small"
    )
    resp = await sdk(api_key).embeddings.create(model="m", input="hello world")
    # OpenAI-shaped embedding response the SDK parses for any RAG framework.
    assert resp.data[0].embedding == [0.1, 0.2, 0.3]
    assert resp.data[0].index == 0
    assert resp.usage.prompt_tokens >= 0
    # The input reached the upstream provider (not dropped by the allowlist).
    assert FakeClient.last_kwargs["input"] == "hello world"


async def test_embeddings_accepts_a_list_input(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client, model_type="embeddings", provider_model_id="text-embedding-3-small"
    )
    resp = await sdk(api_key).embeddings.create(model="m", input=["a", "b"])
    assert resp.data  # a batch request returns at least one vector
    assert FakeClient.last_kwargs["input"] == ["a", "b"]
