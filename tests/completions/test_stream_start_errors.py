"""R7-H24: a provider failure at stream *open* must surface as an HTTP error
status, not a 200 with an aborted body. The completion service primes the first
chunk before returning the SSE response, so translate_upstream_error's mapping
(here httpx.ConnectError -> UpstreamUnavailable -> 502) reaches the client."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from litestar.status_codes import HTTP_502_BAD_GATEWAY
from litestar.testing import AsyncTestClient

from litestar_gateway.infrastructure.llm import (
    anthropic_adapter,
    openai_adapter,
    vertex_adapter,
)

from .conftest import ANTHROPIC_VALUES, VERTEX_VALUES, _bearer, _patch, _setup


class _RaisingOpenAI:
    def __init__(self, **kwargs) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def close(self) -> None:
        return None

    async def _create(self, **kwargs):
        raise httpx.ConnectError("upstream down")


class _RaisingAnthropic:
    def __init__(self, **kwargs) -> None:
        self.messages = SimpleNamespace(create=self._create)

    async def close(self) -> None:
        return None

    async def _create(self, **kwargs):
        raise httpx.ConnectError("upstream down")


class _RaisingGenai:
    def __init__(self, **kwargs) -> None:
        async def _raise(**kw):
            raise httpx.ConnectError("upstream down")

        self.aio = SimpleNamespace(
            models=SimpleNamespace(generate_content_stream=_raise), aclose=self._aclose
        )

    async def _aclose(self) -> None:
        return None

    def close(self) -> None:
        return None


async def test_openai_stream_open_error_is_a_502(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(client)
    monkeypatch.setattr(openai_adapter, "AsyncOpenAI", _RaisingOpenAI)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [], "stream": True},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_502_BAD_GATEWAY, resp.text


async def test_anthropic_stream_open_error_is_a_502(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="anthropic", values=ANTHROPIC_VALUES, provider_model_id="claude-3-5-sonnet"
    )
    monkeypatch.setattr(anthropic_adapter, "AsyncAnthropic", _RaisingAnthropic)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [], "stream": True},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_502_BAD_GATEWAY, resp.text


async def test_vertex_stream_open_error_is_a_502(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Vertex additionally used to emit a synthetic role chunk *before* calling
    # the provider, so priming alone wouldn't catch its start error unless the
    # chunk is emitted only after generate_content_stream succeeds.
    _patch(monkeypatch)
    api_key = await _setup(
        client, provider="vertex_ai", values=VERTEX_VALUES, provider_model_id="gemini-1.5-pro"
    )
    monkeypatch.setattr(vertex_adapter.genai, "Client", _RaisingGenai)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [], "stream": True},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_502_BAD_GATEWAY, resp.text
