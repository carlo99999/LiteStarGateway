"""OpenAI-compatible adapter: registry-backed client reuse (Plan 14 Step 3).

Exercises `OpenAIAdapter`/`AzureOpenAIAdapter` against a real `ClientRegistry`
with a fake async client standing in for `AsyncOpenAI`/`AsyncAzureOpenAI` (no
real SDK construction, no network) — the adapter's `_client_key`/`_async_client`
wiring is what's under test, not the OpenAI SDK itself.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from litestar_gateway.domain.entities import Model
from litestar_gateway.domain.entities.enums import ModelType, Provider
from litestar_gateway.infrastructure.llm.azure_adapter import AzureOpenAIAdapter
from litestar_gateway.infrastructure.llm.client_registry import ClientRegistry
from litestar_gateway.infrastructure.llm.openai_adapter import OpenAIAdapter
from litestar_gateway.infrastructure.llm.resilience import ResilienceConfig


def _model(**overrides: Any) -> Model:
    defaults: dict[str, Any] = dict(
        id=uuid4(),
        team_id=uuid4(),
        name="fast-chat",
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gpt-4o",
        params={},
        api_version=None,
        input_cost_per_token=None,
        output_cost_per_token=None,
        enabled=True,
        created_at=None,
    )
    defaults.update(overrides)
    return Model(**defaults)


class _FakeCompletions:
    def __init__(self, response_factory: Any) -> None:
        self._response_factory = response_factory

    async def create(self, **kwargs: Any) -> Any:
        return self._response_factory(kwargs)


class _FakeChat:
    def __init__(self, response_factory: Any) -> None:
        self.completions = _FakeCompletions(response_factory)


class _FakeAsyncClient:
    def __init__(self, response_factory: Any) -> None:
        self.chat = _FakeChat(response_factory)
        self.closed = False
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1
        self.closed = True


class _Completion:
    def model_dump(self) -> dict[str, Any]:
        return {"choices": []}


class _Chunk:
    def __init__(self, index: int) -> None:
        self._index = index

    def model_dump(self) -> dict[str, Any]:
        return {"index": self._index}


class _FakeStream:
    """Async iterator over chunks; can pause mid-iteration for cancellation tests."""

    def __init__(self, count: int, *, gate: asyncio.Event | None = None) -> None:
        self._remaining = count
        self._gate = gate
        self._yielded = 0

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> _Chunk:
        if self._remaining <= 0:
            raise StopAsyncIteration
        if self._gate is not None and self._yielded == 1:
            await self._gate.wait()  # hang here until the test cancels us
        self._remaining -= 1
        self._yielded += 1
        return _Chunk(self._yielded)


def _default_response_factory(kwargs: dict[str, Any]) -> Any:
    if kwargs.get("stream"):
        return _FakeStream(3)
    return _Completion()


def _patch_async_client(
    monkeypatch: pytest.MonkeyPatch,
    adapter_cls: type,
    *,
    response_factory: Any = _default_response_factory,
) -> list[_FakeAsyncClient]:
    created: list[_FakeAsyncClient] = []

    def _fake_async_client(self: Any, model: Model, credentials: dict[str, str]) -> Any:
        client = _FakeAsyncClient(response_factory)
        created.append(client)
        return client

    monkeypatch.setattr(adapter_cls, "_async_client", _fake_async_client)
    return created


async def test_sequential_chat_completions_reuse_one_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ClientRegistry(close=lambda c: c.close())
    created = _patch_async_client(monkeypatch, OpenAIAdapter)
    adapter = OpenAIAdapter(ResilienceConfig(), registry)
    model = _model()
    credentials = {"api_key": "sk-test"}  # pragma: allowlist secret

    await adapter.achat_completion({"messages": []}, model, credentials)
    await adapter.achat_completion({"messages": []}, model, credentials)

    assert len(created) == 1  # second call reused the leased client
    assert created[0].close_count == 0  # never closed while still cached
    await registry.aclose()
    assert created[0].close_count == 1


async def test_rotated_credentials_get_a_new_client(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ClientRegistry(close=lambda c: c.close())
    created = _patch_async_client(monkeypatch, OpenAIAdapter)
    adapter = OpenAIAdapter(ResilienceConfig(), registry)
    model = _model()

    old_credentials = {"api_key": "sk-old"}  # pragma: allowlist secret
    new_credentials = {"api_key": "sk-new"}  # pragma: allowlist secret
    await adapter.achat_completion({"messages": []}, model, old_credentials)
    await adapter.achat_completion({"messages": []}, model, new_credentials)

    assert len(created) == 2
    assert created[0] is not created[1]
    assert created[0].close_count == 0  # rotation alone doesn't force-close


async def test_stream_cancellation_releases_lease_without_closing_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ClientRegistry(close=lambda c: c.close())
    gate = asyncio.Event()
    created = _patch_async_client(
        monkeypatch,
        OpenAIAdapter,
        response_factory=lambda kwargs: _FakeStream(5, gate=gate),
    )
    adapter = OpenAIAdapter(ResilienceConfig(), registry)
    model = _model()
    credentials = {"api_key": "sk-test"}  # pragma: allowlist secret

    # A concurrent non-streaming call keeps the same client leased throughout,
    # simulating another in-flight request sharing the connection.
    async def hold_lease() -> None:
        async with adapter._leased_async_client(model, credentials):
            await asyncio.sleep(0.2)

    holder = asyncio.create_task(hold_lease())
    await asyncio.sleep(0.01)

    collected: list[dict[str, Any]] = []

    async def consume() -> None:
        async for chunk in adapter.astream_chat_completion({"messages": []}, model, credentials):
            collected.append(chunk)

    stream_task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)  # let it yield the first chunk, then hang on the gate
    stream_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stream_task

    assert len(collected) == 1  # cancelled after exactly one chunk
    assert len(created) == 1  # the streaming call reused hold_lease()'s client
    assert created[0].closed is False  # still leased by hold_lease()

    await holder  # release the other lease
    assert created[0].closed is False  # only cached, not force-closed

    await registry.aclose()
    assert created[0].close_count == 1


async def test_azure_client_key_isolates_api_version(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ClientRegistry(close=lambda c: c.close())
    created = _patch_async_client(monkeypatch, AzureOpenAIAdapter)
    adapter = AzureOpenAIAdapter(ResilienceConfig(), registry)
    model = _model(provider=Provider.AZURE_OPENAI, api_version="2024-01-01")
    base_credentials = {
        "api_key": "sk-test",  # pragma: allowlist secret
        "api_base": "https://example.openai.azure.com",
    }

    await adapter.achat_completion(
        {"messages": []}, model, {**base_credentials, "api_version": "2024-01-01"}
    )
    await adapter.achat_completion(
        {"messages": []}, model, {**base_credentials, "api_version": "2024-06-01"}
    )

    assert len(created) == 2  # distinct api_version -> distinct client, never shared
