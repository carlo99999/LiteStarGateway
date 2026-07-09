"""Native Gemini generateContent wire-contract conformance harness (Plan 01,
native extension of the OpenAI/Anthropic contract suites).

The gateway is compatible with *any* Gemini-native client (the `google-genai`
SDK, Pydantic AI's `GeminiModel`, …) iff
`POST /v1beta/models/{model}:generateContent` (and `:streamGenerateContent`)
speaks the Gemini Developer-API wire protocol faithfully — native passthrough,
no OpenAI translation. This suite asserts that **contract** (response
`candidates`/`content`/`parts`/`usageMetadata` shape, the `functionCall` /
`functionResponse` round-trip, and the streaming chunk shape) and drives it with
the official `google-genai` Python SDK as an end-to-end **canary**, never a
per-framework matrix. There is zero per-framework code here.

Wiring: the real SDK talks to the in-process Litestar app over
`httpx.ASGITransport(app=<app>)` — injected through
`HttpOptions(base_url=…, async_client_args={"transport": …})`, which `genai`
forwards to its underlying `httpx.AsyncClient`. No server, no socket, no live
API key. The SDK authenticates with `api_key=<gateway team key>`, which it sends
as the `x-goog-api-key` header the gateway's shared middleware accepts, and it
targets the Developer-API path `/v1beta/models/<alias>:generateContent`. The
upstream Gemini client the *gateway* calls is faked (the `Fake*` pattern from
`tests/completions/`), so every response is deterministic and offline.

Two distinct `genai.Client` layers, do not conflate them:
  * the SDK **under test** (a real `genai.Client`, captured before patching) →
    the client any framework uses;
  * the upstream `genai.Client` **inside the adapter** (faked here) → the
    provider. Patching `vertex_adapter.genai.Client` replaces the module-level
    `google.genai.Client`, so `_RealGenaiClient` is bound at import time to keep
    the SDK-under-test pointed at the real implementation.

Impedance note: Phase 2a builds the *upstream* client in `vertexai=True` mode
while the client under test speaks the Gemini Developer-API shape. Both API
surfaces share the same `GenerateContentResponse` proto (camelCase `candidates`
/ `content` / `parts` / `usageMetadata` / `functionCall`), so a Developer-API
SDK parses a response the gateway relays verbatim without a shape mismatch —
which is exactly what the wire-shape and function-call assertions below prove.
The upstream is faked, so this contract is asserted against that shared shape,
not against a live Vertex endpoint.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from completions.conftest import VERTEX_VALUES, _setup_team, _team_usage

# Capture the real Client class BEFORE any test patches `vertex_adapter.genai`
# (which aliases `google.genai`); this binding is the SDK under test.
from google.genai import Client as _RealGenaiClient
from google.genai.types import GenerateContentConfig, HttpOptions, Tool
from litestar.testing import AsyncTestClient

# Re-export the completions harness `client` fixture (same app, same team/model/
# key setup). `tests/` is on sys.path during collection, so `completions` and
# `conformance` import as top-level packages (mirrors tests/native/conftest.py).
from conformance.conftest import client as client  # noqa: F401  (fixture re-export)
from litestar_gateway.infrastructure.llm import vertex_adapter

# The team model behind the native endpoint must be Vertex-backed.
_MODEL_ID = "gemini-1.5-pro-002"

# A well-formed Gemini tool spec (the shape a compliant native client sends).
WEATHER_TOOL = Tool(
    function_declarations=[
        {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]
)


def _has_function_response(contents: list[Any]) -> bool:
    """A function-call round-trip is in progress once the conversation carries a
    `functionResponse` part (the Gemini shape for answering a functionCall)."""
    for message in contents:
        parts = message.get("parts") if isinstance(message, dict) else None
        if isinstance(parts, list) and any(
            isinstance(part, dict) and "functionResponse" in part for part in parts
        ):
            return True
    return False


def _function_call_response() -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"functionCall": {"name": "get_weather", "args": {"city": "Paris"}}}],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 15,
            "totalTokenCount": 25,
        },
        "modelVersion": _MODEL_ID,
        "responseId": "resp-fc",
    }


def _final_text_response() -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "It is sunny in Paris."}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 8, "totalTokenCount": 28},
        "modelVersion": _MODEL_ID,
        "responseId": "resp-final",
    }


def _text_stream_chunks() -> list[dict[str, Any]]:
    """A well-formed Gemini stream: text parts arrive across chunks and the final
    chunk carries the (cumulative) usageMetadata the meter bills on."""
    return [
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "Hello"}]}}]},
        {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": " world"}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 7,
                "totalTokenCount": 12,
            },
        },
    ]


class _FakeGeminiApiClient:
    """Fakes the `google-genai` low-level request surface the native passthrough
    calls (`client.aio._api_client.async_request` / `async_request_streamed`).

    Emits native Gemini REST bodies. With a `functionResponse` already in the
    conversation it returns the final text answer; otherwise, when `tools` are
    present, it returns a `functionCall`; plain requests get final text. Captures
    the URL path (model-in-path resolution) and the verbatim request body, and
    echoes the raw REST JSON as a `.body` string like the real SdkHttpResponse."""

    last_path: str = ""
    last_body: dict[str, Any] = {}

    async def async_request(
        self, http_method: str, path: str, request_dict: dict[str, Any], http_options: object = None
    ) -> SimpleNamespace:
        _FakeGeminiApiClient.last_path = path
        _FakeGeminiApiClient.last_body = request_dict
        contents = request_dict.get("contents") or []
        if request_dict.get("tools") and not _has_function_response(contents):
            body = _function_call_response()
        else:
            body = _final_text_response()
        return SimpleNamespace(headers={}, body=json.dumps(body))

    async def async_request_streamed(
        self, http_method: str, path: str, request_dict: dict[str, Any], http_options: object = None
    ) -> AsyncIterator[SimpleNamespace]:
        _FakeGeminiApiClient.last_path = path
        _FakeGeminiApiClient.last_body = request_dict

        async def gen() -> AsyncIterator[SimpleNamespace]:
            for chunk in _text_stream_chunks():
                yield SimpleNamespace(headers={}, body=json.dumps(chunk))

        return gen()


class FakeUpstream:
    """Fake upstream `genai.Client` the *gateway* calls (built in vertexai mode by
    the adapter). Exposes only the low-level async surface the native passthrough
    uses: `aio._api_client` and `aio.aclose`."""

    def __init__(self, **kwargs: Any) -> None:
        self.aio = SimpleNamespace(_api_client=_FakeGeminiApiClient(), aclose=self._aclose)

    def close(self) -> None:  # genai.Client.close is sync
        return None

    async def _aclose(self) -> None:  # the async surface closes via client.aio.aclose()
        return None


def _patch_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the Vertex adapter's upstream `genai.Client` at the fake. This
    replaces the module-level `google.genai.Client`; the SDK under test uses the
    `_RealGenaiClient` reference captured at import, so it is unaffected."""
    monkeypatch.setattr(vertex_adapter.genai, "Client", FakeUpstream)
    _FakeGeminiApiClient.last_path = ""
    _FakeGeminiApiClient.last_body = {}


def _genai_sdk(test_client: AsyncTestClient, api_key: str) -> Any:
    """The official `google-genai` SDK, wired to the in-process app over ASGI
    transport. `base_url` is the gateway root so the SDK targets
    `/v1beta/models/<alias>:generateContent`; `api_key` is sent as
    `x-goog-api-key` (the gateway accepts it). The ASGI transport is injected via
    `async_client_args`, which `genai` forwards to its `httpx.AsyncClient`."""
    return _RealGenaiClient(
        api_key=api_key,
        http_options=HttpOptions(
            base_url="http://gateway",
            async_client_args={"transport": httpx.ASGITransport(app=test_client.app)},
        ),
    )


@pytest.fixture
async def sdk(
    client: AsyncTestClient,
) -> AsyncIterator[Callable[[str], Any]]:
    """Factory: `sdk(api_key)` -> a `google-genai` client bound to the running app."""
    created: list[Any] = []

    def factory(api_key: str) -> Any:
        sdk_client = _genai_sdk(client, api_key)
        created.append(sdk_client)
        return sdk_client

    yield factory
    for sdk_client in created:
        await sdk_client.aio.aclose()


async def _gemini_key(client: AsyncTestClient, **overrides: Any) -> tuple[str, str, str]:
    return await _setup_team(
        client,
        provider="vertex_ai",
        values=VERTEX_VALUES,
        provider_model_id=_MODEL_ID,
        model_type="chat",
        **overrides,
    )


async def test_response_wire_shape(
    client: AsyncTestClient,
    sdk: Callable[[str], Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The native contract: a `GenerateContentResponse` with `candidates` carrying
    # `content.parts` text and Gemini-native `usageMetadata`
    # (promptTokenCount/candidatesTokenCount) — NOT the OpenAI
    # choices/finish_reason/prompt_tokens shape.
    _patch_upstream(monkeypatch)
    key, _, _ = await _gemini_key(client)
    response = await sdk(key).aio.models.generate_content(model="m", contents="hello")
    assert response.text == "It is sunny in Paris."
    assert response.candidates[0].content.parts[0].text == "It is sunny in Paris."
    assert response.usage_metadata.prompt_token_count == 20
    assert response.usage_metadata.candidates_token_count == 8
    # The team alias was resolved to the upstream provider id in the URL PATH
    # (passthrough, not translation), and the body flowed through untouched — the
    # native `contents` shape, never OpenAI `messages`.
    assert _FakeGeminiApiClient.last_path == f"{_MODEL_ID}:generateContent"
    assert "contents" in _FakeGeminiApiClient.last_body
    assert "messages" not in _FakeGeminiApiClient.last_body


async def test_function_call_round_trip(
    client: AsyncTestClient,
    sdk: Callable[[str], Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The full native tool loop: request with `tools` -> `functionCall` part ->
    # client returns a `functionResponse` -> final text. Proves function
    # declarations pass through untranslated and a functionResponse turn is
    # accepted and relayed upstream.
    _patch_upstream(monkeypatch)
    key, _, _ = await _gemini_key(client)
    genai_client = sdk(key)
    config = GenerateContentConfig(tools=[WEATHER_TOOL])

    # Turn 1: the model asks for the tool.
    first = await genai_client.aio.models.generate_content(
        model="m", contents="What is the weather in Paris?", config=config
    )
    call = first.candidates[0].content.parts[0].function_call
    assert call.name == "get_weather"
    assert dict(call.args) == {"city": "Paris"}
    # Function declarations were forwarded to the provider unchanged (Gemini's
    # `functionDeclarations` shape), not dropped or translated.
    tools_sent = _FakeGeminiApiClient.last_body["tools"]
    assert tools_sent[0]["functionDeclarations"][0]["name"] == "get_weather"

    # Turn 2: feed the model turn + the tool's result back (the round-trip).
    contents = [
        {"role": "user", "parts": [{"text": "What is the weather in Paris?"}]},
        first.candidates[0].content.model_dump(exclude_none=True),
        {
            "role": "user",
            "parts": [
                {"functionResponse": {"name": "get_weather", "response": {"weather": "sunny"}}}
            ],
        },
    ]
    second = await genai_client.aio.models.generate_content(
        model="m", contents=contents, config=config
    )
    assert second.text == "It is sunny in Paris."
    # The gateway relayed the functionResponse turn upstream (the round-trip
    # reached it).
    assert _has_function_response(_FakeGeminiApiClient.last_body["contents"])


async def test_streaming_accumulates(
    client: AsyncTestClient,
    sdk: Callable[[str], Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The raw stream relays native Gemini chunks (the SDK parses them from the
    # gateway's `data:` SSE at `?alt=sse`), accumulating to text and a final
    # usageMetadata — never OpenAI chunks.
    _patch_upstream(monkeypatch)
    key, _, _ = await _gemini_key(client)
    parts: list[str] = []
    last_usage = None
    async for chunk in await sdk(key).aio.models.generate_content_stream(model="m", contents="hi"):
        if chunk.text:
            parts.append(chunk.text)
        if chunk.usage_metadata is not None:
            last_usage = chunk.usage_metadata
    assert "".join(parts) == "Hello world"
    assert last_usage is not None
    assert last_usage.prompt_token_count == 5
    assert last_usage.candidates_token_count == 7
    # Resolved + dispatched on the streaming path (alias -> provider id in PATH).
    assert _FakeGeminiApiClient.last_path == f"{_MODEL_ID}:streamGenerateContent"


async def test_round_trip_bills_native_tokens(
    client: AsyncTestClient,
    sdk: Callable[[str], Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Governance holds on the native path: each native call is metered once on the
    # native token counts. The two-turn tool loop records two calls, summing the
    # native promptTokenCount (10 + 20) and candidatesTokenCount (15 + 8) at the
    # model's rate.
    _patch_upstream(monkeypatch)
    key, team, admin = await _gemini_key(
        client, input_cost_per_token=0.01, output_cost_per_token=0.01
    )
    genai_client = sdk(key)
    config = GenerateContentConfig(tools=[WEATHER_TOOL])
    first = await genai_client.aio.models.generate_content(
        model="m", contents="What is the weather in Paris?", config=config
    )
    contents = [
        {"role": "user", "parts": [{"text": "What is the weather in Paris?"}]},
        first.candidates[0].content.model_dump(exclude_none=True),
        {
            "role": "user",
            "parts": [
                {"functionResponse": {"name": "get_weather", "response": {"weather": "sunny"}}}
            ],
        },
    ]
    await genai_client.aio.models.generate_content(model="m", contents=contents, config=config)
    usage = await _team_usage(client, team, admin)
    assert len(usage) == 1
    (row,) = usage
    assert row["calls"] == 2
    assert row["prompt_tokens"] == 30  # native promptTokenCount: 10 + 20
    assert row["completion_tokens"] == 23  # native candidatesTokenCount: 15 + 8
    assert row["cost"] == pytest.approx((30 + 23) * 0.01)
