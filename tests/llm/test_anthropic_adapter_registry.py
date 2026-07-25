"""Anthropic adapter: registry-backed client reuse (Plan 14 Step 4).

Mirrors tests/llm/test_openai_adapter_registry.py but against
AnthropicAdapter/AsyncAnthropic (faked, no real SDK construction/network).
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from litestar_gateway.domain.entities import Model
from litestar_gateway.domain.entities.enums import ModelType, Provider
from litestar_gateway.infrastructure.llm.anthropic_adapter import AnthropicAdapter
from litestar_gateway.infrastructure.llm.client_registry import ClientRegistry
from litestar_gateway.infrastructure.llm.resilience import ResilienceConfig


def _model(**overrides: Any) -> Model:
    defaults: dict[str, Any] = dict(
        id=uuid4(),
        team_id=uuid4(),
        name="claude-chat",
        provider=Provider.ANTHROPIC,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="claude-3-5-sonnet",
        params={},
        api_version=None,
        input_cost_per_token=None,
        output_cost_per_token=None,
        enabled=True,
        created_at=None,
    )
    defaults.update(overrides)
    return Model(**defaults)


class _FakeMessage:
    def model_dump(self) -> dict[str, Any]:
        return {
            "id": "msg_1",
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }


class _StreamEvent:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, Any]:
        return self._payload


class _FakeStream:
    def __init__(self, events: list[dict[str, Any]], *, gate: asyncio.Event | None = None) -> None:
        self._events = list(events)
        self._gate = gate
        self._yielded = 0

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> _StreamEvent:
        if not self._events:
            raise StopAsyncIteration
        if self._gate is not None and self._yielded == 1:
            await self._gate.wait()
        self._yielded += 1
        return _StreamEvent(self._events.pop(0))


def _default_response_factory(kwargs: dict[str, Any]) -> Any:
    if kwargs.get("stream"):
        return _FakeStream(
            [
                {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}},
            ]
        )
    return _FakeMessage()


class _FakeMessages:
    def __init__(self, response_factory: Any) -> None:
        self._response_factory = response_factory

    async def create(self, **kwargs: Any) -> Any:
        return self._response_factory(kwargs)


class _FakeAnthropicClient:
    def __init__(self, response_factory: Any) -> None:
        self.messages = _FakeMessages(response_factory)
        self.closed = False
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1
        self.closed = True


def _patch_async_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response_factory: Any = _default_response_factory,
) -> list[_FakeAnthropicClient]:
    created: list[_FakeAnthropicClient] = []

    def _fake_async_client(self: Any, credentials: dict[str, str]) -> Any:
        client = _FakeAnthropicClient(response_factory)
        created.append(client)
        return client

    monkeypatch.setattr(AnthropicAdapter, "_async_client", _fake_async_client)
    return created


async def test_sequential_chat_completions_reuse_one_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ClientRegistry(close=lambda c: c.close())
    created = _patch_async_client(monkeypatch)
    adapter = AnthropicAdapter(ResilienceConfig(), registry)
    model = _model()
    credentials = {"api_key": "sk-ant-test"}  # pragma: allowlist secret
    request = {"messages": [{"role": "user", "content": "hi"}]}

    await adapter.achat_completion(request, model, credentials)
    await adapter.achat_completion(request, model, credentials)

    assert len(created) == 1
    assert created[0].close_count == 0
    await registry.aclose()
    assert created[0].close_count == 1


async def test_rotated_credentials_get_a_new_client(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ClientRegistry(close=lambda c: c.close())
    created = _patch_async_client(monkeypatch)
    adapter = AnthropicAdapter(ResilienceConfig(), registry)
    model = _model()

    old_credentials = {"api_key": "sk-ant-old"}  # pragma: allowlist secret
    new_credentials = {"api_key": "sk-ant-new"}  # pragma: allowlist secret
    request = {"messages": [{"role": "user", "content": "hi"}]}
    await adapter.achat_completion(request, model, old_credentials)
    await adapter.achat_completion(request, model, new_credentials)

    assert len(created) == 2
    assert created[0] is not created[1]
    assert created[0].close_count == 0


async def test_stream_cancellation_releases_lease_without_closing_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ClientRegistry(close=lambda c: c.close())
    gate = asyncio.Event()
    created = _patch_async_client(
        monkeypatch,
        response_factory=lambda kwargs: _FakeStream(
            [
                {"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "a"}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "b"}},
            ],
            gate=gate,
        ),
    )
    adapter = AnthropicAdapter(ResilienceConfig(), registry)
    model = _model()
    credentials = {"api_key": "sk-ant-test"}  # pragma: allowlist secret

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
