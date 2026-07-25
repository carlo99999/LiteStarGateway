"""Vertex adapter: registry-backed client reuse (Plan 14 Step 4).

Mirrors tests/llm/test_openai_adapter_registry.py but against
VertexAdapter/genai.Client (faked, no real SDK construction/network). Vertex
closes async clients via `client.aio.aclose()`, not `client.close()` — the
registry instance here uses that close callback, same as `gateway.py` wires
for its dedicated Vertex registry.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from litestar_gateway.domain.entities import Model
from litestar_gateway.domain.entities.enums import ModelType, Provider
from litestar_gateway.infrastructure.llm.client_registry import ClientRegistry
from litestar_gateway.infrastructure.llm.resilience import ResilienceConfig
from litestar_gateway.infrastructure.llm.vertex_adapter import VertexAdapter


def _model(**overrides: Any) -> Model:
    defaults: dict[str, Any] = dict(
        id=uuid4(),
        team_id=uuid4(),
        name="gemini-chat",
        provider=Provider.VERTEX_AI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gemini-1.5-pro",
        params={},
        api_version=None,
        input_cost_per_token=None,
        output_cost_per_token=None,
        enabled=True,
        created_at=None,
    )
    defaults.update(overrides)
    return Model(**defaults)


class _FakeResponse:
    def model_dump(self) -> dict[str, Any]:
        return {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "hi"}]},
                    "finish_reason": "STOP",
                }
            ],
            "usage_metadata": {
                "prompt_token_count": 1,
                "candidates_token_count": 1,
                "total_token_count": 2,
            },
        }


class _StreamChunk:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, Any]:
        return self._payload


class _FakeStream:
    def __init__(self, chunks: list[dict[str, Any]], *, gate: asyncio.Event | None = None) -> None:
        self._chunks = list(chunks)
        self._gate = gate
        self._yielded = 0

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> _StreamChunk:
        if not self._chunks:
            raise StopAsyncIteration
        if self._gate is not None and self._yielded == 1:
            await self._gate.wait()
        self._yielded += 1
        return _StreamChunk(self._chunks.pop(0))


def _default_response_factory(kwargs: dict[str, Any]) -> Any:
    return _FakeResponse()


class _FakeModels:
    def __init__(self, response_factory: Any, stream_factory: Any) -> None:
        self._response_factory = response_factory
        self._stream_factory = stream_factory

    async def generate_content(self, **kwargs: Any) -> Any:
        return self._response_factory(kwargs)

    async def generate_content_stream(self, **kwargs: Any) -> Any:
        return self._stream_factory(kwargs)


def _default_stream_factory(kwargs: dict[str, Any]) -> Any:
    return _FakeStream(
        [
            {
                "candidates": [{"content": {"parts": [{"text": "hi"}]}, "finish_reason": None}],
                "usage_metadata": {},
            }
        ]
    )


class _FakeGenaiClient:
    def __init__(self, response_factory: Any, stream_factory: Any) -> None:
        self.aio = SimpleNamespace(
            models=_FakeModels(response_factory, stream_factory), aclose=self._aclose
        )
        self.closed = False
        self.close_count = 0

    async def _aclose(self) -> None:
        self.close_count += 1
        self.closed = True


def _patch_async_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response_factory: Any = _default_response_factory,
    stream_factory: Any = _default_stream_factory,
) -> list[_FakeGenaiClient]:
    created: list[_FakeGenaiClient] = []

    def _fake_async_client(self: Any, credentials: dict[str, str]) -> Any:
        client = _FakeGenaiClient(response_factory, stream_factory)
        created.append(client)
        return client

    monkeypatch.setattr(VertexAdapter, "_async_client", _fake_async_client)
    return created


def _close_genai_client(client: Any) -> Any:
    return client.aio.aclose()


async def test_sequential_chat_completions_reuse_one_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ClientRegistry(close=_close_genai_client)
    created = _patch_async_client(monkeypatch)
    adapter = VertexAdapter(ResilienceConfig(), registry)
    model = _model()
    credentials = {
        "vertex_project": "p",
        "vertex_location": "us-central1",
    }
    request = {"messages": [{"role": "user", "content": "hi"}]}

    await adapter.achat_completion(request, model, credentials)
    await adapter.achat_completion(request, model, credentials)

    assert len(created) == 1
    assert created[0].close_count == 0
    await registry.aclose()
    assert created[0].close_count == 1


async def test_rotated_project_gets_a_new_client(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ClientRegistry(close=_close_genai_client)
    created = _patch_async_client(monkeypatch)
    adapter = VertexAdapter(ResilienceConfig(), registry)
    model = _model()
    request = {"messages": [{"role": "user", "content": "hi"}]}
    first_credentials = {"vertex_project": "p1", "vertex_location": "us"}
    second_credentials = {"vertex_project": "p2", "vertex_location": "us"}

    await adapter.achat_completion(request, model, first_credentials)
    await adapter.achat_completion(request, model, second_credentials)

    assert len(created) == 2
    assert created[0] is not created[1]
    assert created[0].close_count == 0


async def test_stream_cancellation_releases_lease_without_closing_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ClientRegistry(close=_close_genai_client)
    gate = asyncio.Event()
    created = _patch_async_client(
        monkeypatch,
        stream_factory=lambda kwargs: _FakeStream(
            [
                {"candidates": [{"content": {"parts": [{"text": "a"}]}}], "usage_metadata": {}},
                {"candidates": [{"content": {"parts": [{"text": "b"}]}}], "usage_metadata": {}},
            ],
            gate=gate,
        ),
    )
    adapter = VertexAdapter(ResilienceConfig(), registry)
    model = _model()
    credentials = {"vertex_project": "p", "vertex_location": "us-central1"}

    async def hold_lease() -> None:
        async with adapter._leased_async_client(credentials):
            await asyncio.sleep(0.2)

    holder = asyncio.create_task(hold_lease())
    await asyncio.sleep(0.01)

    collected: list[dict[str, Any]] = []

    async def consume() -> None:
        request = {"messages": [{"role": "user", "content": "hi"}]}
        async for chunk in adapter.astream_chat_completion(request, model, credentials):
            collected.append(chunk)

    stream_task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    stream_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stream_task

    assert len(created) == 1  # reused hold_lease()'s client
    assert created[0].closed is False

    await holder
    assert created[0].closed is False

    await registry.aclose()
    assert created[0].close_count == 1
