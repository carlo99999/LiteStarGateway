"""Contract: errors surface to the client with the right HTTP status.

A compliant client must be able to *fail loudly* — the SDK raises a typed
`APIStatusError` carrying the HTTP status, never a hang or a fabricated answer.
Includes the R7-H23 behavior: tools/vision on a provider whose translator cannot
express them (Anthropic/Vertex/Bedrock) returns 501, which the SDK surfaces as an
error.

The OpenAI-shaped error *envelope* (`{"error": {...}}`) is asserted below for
both a route-level domain error (unknown model) and a middleware-level error
(bad API key), so an SDK reading `error.message` works across the surface."""

from __future__ import annotations

from collections.abc import Callable

import openai
import pytest
from completions.conftest import ANTHROPIC_VALUES, _setup
from litestar.testing import AsyncTestClient
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from .conftest import WEATHER_TOOL, _patch_upstream

IMAGE_MESSAGE: ChatCompletionMessageParam = {
    "role": "user",
    "content": [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
    ],
}


async def test_unknown_model_surfaces_404(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_upstream(monkeypatch)
    api_key = await _setup(client)
    with pytest.raises(openai.NotFoundError) as exc:
        await sdk(api_key).chat.completions.create(
            model="does-not-exist", messages=[{"role": "user", "content": "hi"}]
        )
    assert exc.value.status_code == 404


async def test_disabled_model_surfaces_409(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_upstream(monkeypatch)
    api_key = await _setup(client, enabled=False)
    with pytest.raises(openai.ConflictError) as exc:
        await sdk(api_key).chat.completions.create(
            model="m", messages=[{"role": "user", "content": "hi"}]
        )
    assert exc.value.status_code == 409


async def test_bad_api_key_surfaces_401(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_upstream(monkeypatch)
    await _setup(client)
    with pytest.raises(openai.AuthenticationError) as exc:
        await sdk("lsk-not-a-real-key").chat.completions.create(
            model="m", messages=[{"role": "user", "content": "hi"}]
        )
    assert exc.value.status_code == 401


async def test_auth_error_body_is_openai_envelope(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The auth failure is raised by the API-key middleware (before the route),
    # so it must still reach the client as the OpenAI `{"error": {...}}` envelope
    # — not Litestar's default `{"status_code", "detail"}` — or an SDK reading
    # `error.message` would see nothing.
    _patch_upstream(monkeypatch)
    await _setup(client)
    with pytest.raises(openai.AuthenticationError) as exc:
        await sdk("lsk-not-a-real-key").chat.completions.create(
            model="m", messages=[{"role": "user", "content": "hi"}]
        )
    body = exc.value.response.json()
    assert body["error"]["message"]
    assert body["error"]["type"] == "authentication_error"


async def test_h23_tools_on_anthropic_surfaces_501(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # R7-H23: Anthropic's chat translator cannot express tool calling. The gateway
    # rejects with 501 *before* dispatch — the SDK raises, so the client fails
    # loudly instead of getting a silently text-only (fabricated) answer.
    _patch_upstream(monkeypatch)
    api_key = await _setup(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-3-5-sonnet",
    )
    with pytest.raises(openai.APIStatusError) as exc:
        await sdk(api_key).chat.completions.create(
            model="m",
            messages=[{"role": "user", "content": "weather?"}],
            tools=[WEATHER_TOOL],
        )
    assert exc.value.status_code == 501


async def test_h23_vision_on_anthropic_surfaces_501(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same guard covers non-text (image) content the translator would drop.
    _patch_upstream(monkeypatch)
    api_key = await _setup(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id="claude-3-5-sonnet",
    )
    with pytest.raises(openai.APIStatusError) as exc:
        await sdk(api_key).chat.completions.create(model="m", messages=[IMAGE_MESSAGE])
    assert exc.value.status_code == 501


async def test_error_body_is_openai_envelope(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_upstream(monkeypatch)
    api_key = await _setup(client)
    with pytest.raises(openai.NotFoundError) as exc:
        await sdk(api_key).chat.completions.create(
            model="does-not-exist", messages=[{"role": "user", "content": "hi"}]
        )
    body = exc.value.response.json()
    assert "error" in body
    assert "message" in body["error"]
    assert "type" in body["error"]
