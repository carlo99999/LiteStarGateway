"""Tests for the Gemini-native `POST /v1beta/models/{model}:generateContent`
(and `:streamGenerateContent`) endpoints.

The surface mirrors the Anthropic native `/v1/messages` path: fully guarded (auth,
per-IP rate limit, model resolution + enable/type/credential checks) and a native
passthrough — the client's Gemini `GenerateContentRequest` body flows upstream
untranslated and the native REST response is returned verbatim (Gemini shape —
`candidates`/`usageMetadata`, never OpenAI `choices`). The Gemini protocol carries
the model alias in the URL PATH (not the body) and the stock `google-genai` SDK
authenticates with `x-goog-api-key`. The call is metered on the native
`usageMetadata` token counts.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import anyio
import pytest
from completions.conftest import (
    OPENAI_VALUES,
    VERTEX_VALUES,
    _bearer,
    _patch,
    _setup_team,
    _team_usage,
)
from google.genai import errors
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_404_NOT_FOUND,
    HTTP_502_BAD_GATEWAY,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.entities import Model, ModelType, Provider, TraceRecord, UsageEvent
from litestar_gateway.infrastructure.llm import vertex_adapter

_MODEL_ID = "gemini-1.5-pro-002"
# Native Gemini body: `contents` (not OpenAI `messages`); the model alias lives in
# the URL path, so it is absent from the body.
_BODY: dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
_GEN_PATH = "/v1beta/models/m:generateContent"
_STREAM_PATH = "/v1beta/models/m:streamGenerateContent"


class _RaisingGenaiClient:
    """genai.Client stand-in whose low-level request surface raises, to exercise
    the upstream-error normalization on the native path."""

    error: Exception = RuntimeError("unset")

    def __init__(self, **kwargs: object) -> None:
        async def _raise(*args: object, **kw: object) -> Any:
            raise _RaisingGenaiClient.error

        async def _aclose() -> None:
            return None

        api_client = type(
            "_C",
            (),
            {"async_request": staticmethod(_raise), "async_request_streamed": staticmethod(_raise)},
        )()
        self.aio = type("_Aio", (), {"_api_client": api_client, "aclose": staticmethod(_aclose)})()


async def _gemini_team(client: AsyncTestClient, **overrides: Any) -> tuple[str, str, str]:
    return await _setup_team(
        client,
        provider="vertex_ai",
        values=VERTEX_VALUES,
        provider_model_id=_MODEL_ID,
        model_type="chat",
        **overrides,
    )


async def test_unauthenticated_401(client: AsyncTestClient) -> None:
    # No credentials: the shared API-key middleware rejects before any handler.
    resp = await client.post(_GEN_PATH, json=_BODY)
    assert resp.status_code == HTTP_401_UNAUTHORIZED


async def test_x_goog_api_key_header_authenticates(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The native `google-genai` SDK authenticates with an `x-goog-api-key` header
    # (that is what `genai.Client(api_key=...)` sends), NOT `Authorization: Bearer`.
    # The shared API-key middleware must accept it so the documented native-SDK
    # setup works; here the same gateway key delivered via x-goog-api-key reaches
    # native dispatch and returns the verbatim Gemini body (200), not a 401.
    _patch(monkeypatch)
    key, _, _ = await _gemini_team(client)
    resp = await client.post(_GEN_PATH, json=_BODY, headers={"x-goog-api-key": key})
    assert resp.status_code == HTTP_200_OK
    assert "candidates" in resp.json()


async def test_unknown_method_404(client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # A path segment whose method suffix is neither generateContent nor
    # streamGenerateContent is not a Gemini action we serve -> 404.
    _patch(monkeypatch)
    key, _, _ = await _gemini_team(client)
    resp = await client.post("/v1beta/models/m:countTokens", json=_BODY, headers=_bearer(key))
    assert resp.status_code == HTTP_404_NOT_FOUND


async def test_native_passthrough_returns_verbatim_gemini_shape(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Happy path: the native Gemini response is returned verbatim — Gemini shape
    # (candidates/usageMetadata), NOT translated to OpenAI (choices). The team alias
    # is resolved to the upstream model id in the URL PATH and the body flows through
    # untouched (no OpenAI `messages`, no `model` injected).
    _patch(monkeypatch)
    key, _, _ = await _gemini_team(client)
    resp = await client.post(_GEN_PATH, json=_BODY, headers=_bearer(key))
    assert resp.status_code == HTTP_200_OK
    body = resp.json()
    # Native Gemini shape, not OpenAI translation.
    assert "choices" not in body
    assert body["candidates"][0]["content"]["parts"] == [{"text": "ciao"}]
    assert body["usageMetadata"] == {
        "promptTokenCount": 4,
        "candidatesTokenCount": 2,
        "totalTokenCount": 6,
    }
    # Alias -> upstream id resolution happens in the PATH; the body is verbatim.
    assert vertex_adapter.genai.Client.__name__  # patched
    from completions.conftest import _FakeGeminiApiClient

    assert _FakeGeminiApiClient.last_path == f"{_MODEL_ID}:generateContent"
    assert _FakeGeminiApiClient.last_body == _BODY  # verbatim, untranslated


async def test_native_call_bills_one_event_with_native_token_counts(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A priced Vertex model records exactly ONE usage event, billed on the native
    # usageMetadata (promptTokenCount=4 -> input, candidatesTokenCount=2 -> output).
    _patch(monkeypatch)
    key, team, admin = await _gemini_team(
        client, input_cost_per_token=0.01, output_cost_per_token=0.01
    )
    resp = await client.post(_GEN_PATH, json=_BODY, headers=_bearer(key))
    assert resp.status_code == HTTP_200_OK
    usage = await _team_usage(client, team, admin)
    assert len(usage) == 1
    (row,) = usage
    assert row["calls"] == 1
    assert row["prompt_tokens"] == 4  # native promptTokenCount
    assert row["completion_tokens"] == 2  # native candidatesTokenCount
    assert row["cost"] == pytest.approx(0.06)  # (4 + 2) * 0.01


async def test_non_vertex_model_rejected_400(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # generateContent is the Gemini wire shape; a non-Vertex model behind it is a
    # misconfiguration -> ProviderMismatch (400), never dispatched or billed.
    _patch(monkeypatch)
    key, team, admin = await _setup_team(
        client, provider="openai", values=OPENAI_VALUES, model_type="chat"
    )
    resp = await client.post(_GEN_PATH, json=_BODY, headers=_bearer(key))
    assert resp.status_code == HTTP_400_BAD_REQUEST
    assert await _team_usage(client, team, admin) == []


async def test_upstream_error_maps_to_http_status_and_bills_nothing(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An upstream provider failure is normalized to a real HTTP status (not an
    # opaque 500); the reservation is released and nothing is billed.
    _RaisingGenaiClient.error = errors.APIError(
        503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "boom"}}
    )
    monkeypatch.setattr(vertex_adapter.genai, "Client", _RaisingGenaiClient)
    key, team, admin = await _gemini_team(
        client, input_cost_per_token=0.01, output_cost_per_token=0.01
    )
    resp = await client.post(_GEN_PATH, json=_BODY, headers=_bearer(key))
    assert resp.status_code == HTTP_502_BAD_GATEWAY
    assert await _team_usage(client, team, admin) == []


async def test_streaming_relays_raw_gemini_chunks_and_bills_once(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # stream: relays the RAW Gemini chunks as `data:` SSE events (Gemini wire
    # format via ?alt=sse), untranslated — assert Gemini `candidates`/`usageMetadata`,
    # not OpenAI `choices`/`[DONE]`/named events — and bills exactly once at the tail
    # on the native promptTokenCount=4 / candidatesTokenCount=2.
    _patch(monkeypatch)
    key, team, admin = await _gemini_team(
        client, input_cost_per_token=0.01, output_cost_per_token=0.01
    )
    resp = await client.post(_STREAM_PATH, json=_BODY, headers=_bearer(key))
    assert resp.status_code == HTTP_200_OK
    assert "text/event-stream" in resp.headers["content-type"]
    from completions.conftest import _FakeGeminiApiClient

    assert _FakeGeminiApiClient.last_path == f"{_MODEL_ID}:streamGenerateContent"
    body = resp.text
    # Gemini SSE: `data:`-prefixed raw chunks, no event names, no [DONE].
    assert "data:" in body
    assert "candidates" in body
    assert "usageMetadata" in body
    # Raw Gemini shape, NOT OpenAI translation / not Anthropic named events.
    assert "choices" not in body
    assert "chat.completion.chunk" not in body
    assert "[DONE]" not in body
    assert "event: " not in body
    # Billed exactly once at the tail on the native token counts.
    usage = await _team_usage(client, team, admin)
    assert len(usage) == 1
    (row,) = usage
    assert row["calls"] == 1
    assert row["prompt_tokens"] == 4
    assert row["completion_tokens"] == 2


async def test_streaming_open_error_maps_to_http_status_before_commit(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # H24 parity: a provider failure at stream *open* surfaces as a real HTTP status
    # (503 -> 502) BEFORE the SSE 200 commits (the stream is primed), and nothing is
    # billed (reservation released, no usage event).
    _RaisingGenaiClient.error = errors.APIError(
        503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "boom"}}
    )
    monkeypatch.setattr(vertex_adapter.genai, "Client", _RaisingGenaiClient)
    key, team, admin = await _gemini_team(
        client, input_cost_per_token=0.01, output_cost_per_token=0.01
    )
    resp = await client.post(_STREAM_PATH, json=_BODY, headers=_bearer(key))
    assert resp.status_code == HTTP_502_BAD_GATEWAY
    assert await _team_usage(client, team, admin) == []


# --- Native streaming settlement unit tests -------------------------------------
#
# Disconnect-safety (aclose propagation + shielded cancellation) is exercised at
# the CompletionService level with fake ports, mirroring the OpenAI-path
# tests/completions/test_stream_usage_fallback.py — the HTTP client can't interrupt
# an SSE response mid-flight. The native model is Provider.VERTEX_AI so it clears
# open_generate_content_stream's provider guard.

_TEAM_ID = uuid4()
_KEY_ID = uuid4()

# The raw Gemini stream a `streamGenerateContent` request produces (matches the
# native fake in tests/completions/conftest.py): cumulative usageMetadata on the
# final chunk.
_STREAM_CHUNKS: list[dict[str, Any]] = [
    {"candidates": [{"content": {"parts": [{"text": "ci"}]}}]},
    {
        "candidates": [{"content": {"parts": [{"text": "ao"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2, "totalTokenCount": 6},
    },
]


def _vertex_model() -> Model:
    return Model(
        id=uuid4(),
        team_id=_TEAM_ID,
        name="m",
        provider=Provider.VERTEX_AI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id=_MODEL_ID,
        params={},
        api_version=None,
        input_cost_per_token=None,
        output_cost_per_token=None,
        enabled=True,
        created_at=datetime.now(UTC),
    )


class _FakeModels:
    def __init__(self, model: Model) -> None:
        self._model = model

    async def get_by_name(self, team_id: UUID, name: str) -> Model | None:
        return self._model if name == self._model.name else None


class _FakeCredentials:
    async def get_values(self, credential_id: UUID) -> dict[str, str] | None:
        return {"vertex_project": "p", "vertex_location": "us-central1"}


class _FakeUsage:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    async def record(self, event: UsageEvent) -> None:
        # The real repository's first await is a DB roundtrip — a cancellation
        # checkpoint. Mirror it so a cancelled scope re-raises here like in prod.
        await anyio.lowlevel.checkpoint()
        self.events.append(event)

    async def enqueue_pending(self, event: UsageEvent) -> None:  # pragma: no cover
        raise AssertionError("outbox must not be used in these tests")


class _GeminiStreamGateway:
    """Streams a fixed list of raw Gemini chunks for the native path. When `blocked`
    is set it parks at the provider await after the last chunk — where a real client
    disconnect delivers its cancellation."""

    def __init__(self, chunks: list[dict[str, Any]], blocked: anyio.Event | None = None) -> None:
        self._chunks = chunks
        self._blocked = blocked

    async def astream_generate_content(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[dict[str, Any]]:
        for chunk in self._chunks:
            yield chunk
        if self._blocked is not None:
            self._blocked.set()
            await anyio.sleep_forever()


def _native_service(
    gateway: Any, usage: _FakeUsage, traces: list[TraceRecord]
) -> CompletionService:
    return CompletionService(
        models=_FakeModels(_vertex_model()),  # type: ignore[arg-type]
        credentials=_FakeCredentials(),  # type: ignore[arg-type]
        gateway=gateway,
        meter=UsageMeter(usage=usage, emit_trace=traces.append),  # type: ignore[arg-type]
    )


async def test_native_stream_tail_bills_once_with_native_tokens() -> None:
    # A fully-consumed native stream relays the raw Gemini chunks unchanged
    # (Gemini-shaped `candidates`, never OpenAI `choices`) and bills exactly once at
    # the tail on the native promptTokenCount=4 / candidatesTokenCount=2.
    traces: list[TraceRecord] = []
    usage = _FakeUsage()
    service = _native_service(_GeminiStreamGateway(list(_STREAM_CHUNKS)), usage, traces)

    stream = await service.open_generate_content_stream(_TEAM_ID, _KEY_ID, "m", dict(_BODY))
    relayed = [chunk async for chunk in stream]

    assert all("candidates" in c for c in relayed)
    assert all("choices" not in c for c in relayed)
    assert len(usage.events) == 1
    assert usage.events[0].prompt_tokens == 4
    assert usage.events[0].completion_tokens == 2
    assert [t.status for t in traces] == ["ok"]


async def test_native_stream_disconnect_mid_stream_still_settles() -> None:
    # Client disconnect mid-stream (aclose propagates through _rechain), before the
    # final usageMetadata chunk: settle once with the estimation fallback (billing
    # what streamed) rather than leaking the reservation.
    traces: list[TraceRecord] = []
    usage = _FakeUsage()
    service = _native_service(_GeminiStreamGateway(list(_STREAM_CHUNKS)), usage, traces)

    stream = await service.open_generate_content_stream(_TEAM_ID, _KEY_ID, "m", dict(_BODY))
    consumed = 0
    async for _ in stream:
        consumed += 1
        if consumed == 1:  # first chunk (no usageMetadata yet) seen
            break
    assert isinstance(stream, AsyncGenerator)
    await stream.aclose()

    assert len(usage.events) == 1  # settled once, not leaked
    assert [t.status for t in traces] == ["ok"]


async def test_native_stream_cancellation_mid_stream_still_settles() -> None:
    # A real disconnect cancels the streaming scope, raising CancelledError at the
    # provider await. The shielded settlement must still record the usage event
    # (H13 parity) — here all chunks streamed, so it bills native input 4 / output 2.
    traces: list[TraceRecord] = []
    usage = _FakeUsage()
    blocked = anyio.Event()
    service = _native_service(_GeminiStreamGateway(list(_STREAM_CHUNKS), blocked), usage, traces)

    stream = await service.open_generate_content_stream(_TEAM_ID, _KEY_ID, "m", dict(_BODY))

    async def consume() -> None:
        async for _ in stream:
            pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(consume)
        await blocked.wait()  # all chunks streamed; consumer parked at provider await
        tg.cancel_scope.cancel()

    assert len(usage.events) == 1
    assert usage.events[0].prompt_tokens == 4
    assert usage.events[0].completion_tokens == 2
    assert [t.status for t in traces] == ["ok"]
