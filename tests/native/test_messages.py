"""Tests for the Anthropic-native `POST /v1/messages` endpoint.

The surface is fully guarded (auth, per-IP rate limit, model resolution +
enable/type/credential checks) and, from slice 1a on, dispatches non-streaming
requests to the provider as a native passthrough: the client's Anthropic Messages
body flows upstream untranslated and the native response is returned verbatim
(Anthropic shape — `content`/`stop_reason`/`role`, never OpenAI `choices`). The
call is metered on the native `usage` block. Streaming (`stream: true`) is a later
slice and still 501s.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import anthropic
import anyio
import httpx
import pytest
from completions.conftest import (
    ANTHROPIC_VALUES,
    OPENAI_VALUES,
    FakeAnthropic,
    _bearer,
    _patch,
    _setup_team,
    _team_usage,
)
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_402_PAYMENT_REQUIRED,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_502_BAD_GATEWAY,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.entities import Model, ModelType, Provider, TraceRecord, UsageEvent
from litestar_gateway.domain.request_policy import MAX_TOKENS
from litestar_gateway.infrastructure.llm import anthropic_adapter
from litestar_gateway.infrastructure.web.rate_limit import INFERENCE_RATE_LIMIT

_MODEL_ID = "claude-3-5-sonnet-20241022"
_BODY = {"model": "m", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}


class _RaisingAnthropic:
    """AsyncAnthropic stand-in whose messages.create raises, to exercise the
    upstream-error normalization on the native path."""

    error: Exception = RuntimeError("unset")

    def __init__(self, **kwargs: object) -> None:
        self.messages = self

    async def close(self) -> None:
        return None

    async def create(self, **kwargs: object):
        raise _RaisingAnthropic.error


async def test_unauthenticated_401(client: AsyncTestClient) -> None:
    # No bearer token: the shared API-key middleware rejects before any handler.
    resp = await client.post("/v1/messages", json=_BODY)
    assert resp.status_code == HTTP_401_UNAUTHORIZED


async def test_x_api_key_header_authenticates(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The native `anthropic` SDK authenticates with an `x-api-key` header (that is
    # what `Anthropic(api_key=...)` sends), NOT `Authorization: Bearer`. The shared
    # API-key middleware must accept it so the documented native-SDK setup works;
    # here the same gateway key delivered via x-api-key reaches native dispatch and
    # returns the verbatim Anthropic body (200), not a 401.
    _patch(monkeypatch)
    key, _, _ = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id=_MODEL_ID,
        model_type="chat",
    )
    resp = await client.post("/v1/messages", json=_BODY, headers={"x-api-key": key})
    assert resp.status_code == HTTP_200_OK
    assert resp.json()["role"] == "assistant"


async def test_over_rate_limit_429(client: AsyncTestClient) -> None:
    # The per-IP inference limiter runs *before* auth (same middleware the
    # OpenAI endpoints use), so unauthenticated floods are throttled: the budget
    # of requests is let through to the 401, and the next one is blocked.
    limit = INFERENCE_RATE_LIMIT[1]
    for _ in range(limit):
        resp = await client.post("/v1/messages", json=_BODY)
        assert resp.status_code == HTTP_401_UNAUTHORIZED
    blocked = await client.post("/v1/messages", json=_BODY)
    assert blocked.status_code == HTTP_429_TOO_MANY_REQUESTS


async def test_unknown_model_404(client: AsyncTestClient) -> None:
    key, _, _ = await _setup_team(
        client, provider="anthropic", values=ANTHROPIC_VALUES, model_type="chat"
    )
    resp = await client.post("/v1/messages", json={**_BODY, "model": "nope"}, headers=_bearer(key))
    assert resp.status_code == HTTP_404_NOT_FOUND


async def test_disabled_model_409(client: AsyncTestClient) -> None:
    key, _, _ = await _setup_team(
        client, provider="anthropic", values=ANTHROPIC_VALUES, model_type="chat", enabled=False
    )
    resp = await client.post("/v1/messages", json=_BODY, headers=_bearer(key))
    assert resp.status_code == HTTP_409_CONFLICT


async def test_streaming_relays_raw_anthropic_events_and_bills_once(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # stream: true relays the RAW Anthropic events as named SSE events (Anthropic
    # wire format), untranslated — assert Anthropic event `type`s, not OpenAI
    # `choices`/`chat.completion.chunk`/`[DONE]` — and bills exactly once at the
    # tail on the native input_tokens=3 / output_tokens=5 from the fake stream.
    _patch(monkeypatch)
    key, team, admin = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id=_MODEL_ID,
        model_type="chat",
        input_cost_per_token=0.01,
        output_cost_per_token=0.01,
    )
    resp = await client.post("/v1/messages", json={**_BODY, "stream": True}, headers=_bearer(key))
    assert resp.status_code == HTTP_200_OK
    assert "text/event-stream" in resp.headers["content-type"]
    assert FakeAnthropic.last_kwargs["stream"] is True
    assert FakeAnthropic.last_kwargs["model"] == _MODEL_ID
    body = resp.text
    # Anthropic SSE wire format: named events (`event: <type>`), not OpenAI `data:`-only.
    assert "event: message_start" in body
    assert "event: content_block_delta" in body
    assert "event: message_delta" in body
    assert "event: message_stop" in body
    # Raw Anthropic event shape, NOT OpenAI translation.
    assert "text_delta" in body
    assert "choices" not in body
    assert "chat.completion.chunk" not in body
    assert "[DONE]" not in body
    # Billed exactly once at the tail on the native token counts.
    usage = await _team_usage(client, team, admin)
    assert len(usage) == 1
    (row,) = usage
    assert row["calls"] == 1
    assert row["prompt_tokens"] == 3  # native input_tokens
    assert row["completion_tokens"] == 5  # native output_tokens


async def test_streaming_open_error_maps_to_http_status_before_commit(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # H24 parity: a provider failure at stream *open* surfaces as a real HTTP
    # status (503 -> 502) BEFORE the SSE 200 commits (the stream is primed), and
    # nothing is billed (reservation released, no usage event).
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    _RaisingAnthropic.error = anthropic.APIStatusError(
        "boom", response=httpx.Response(503, request=request), body=None
    )
    monkeypatch.setattr(anthropic_adapter, "AsyncAnthropic", _RaisingAnthropic)
    key, team, admin = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id=_MODEL_ID,
        model_type="chat",
        input_cost_per_token=0.01,
        output_cost_per_token=0.01,
    )
    resp = await client.post("/v1/messages", json={**_BODY, "stream": True}, headers=_bearer(key))
    assert resp.status_code == HTTP_502_BAD_GATEWAY
    assert await _team_usage(client, team, admin) == []


async def test_native_passthrough_returns_verbatim_anthropic_shape(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Happy path: the native Anthropic response is returned verbatim — Anthropic
    # shape (content/stop_reason/role), NOT translated to OpenAI (choices). The
    # team alias is resolved to the upstream model id and max_tokens flows through.
    _patch(monkeypatch)
    key, _, _ = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id=_MODEL_ID,
        model_type="chat",
    )
    resp = await client.post("/v1/messages", json=_BODY, headers=_bearer(key))
    assert resp.status_code == HTTP_200_OK
    body = resp.json()
    # Native Anthropic shape, not OpenAI translation.
    assert "choices" not in body
    assert body["role"] == "assistant"
    assert body["stop_reason"] == "end_turn"
    assert body["content"] == [{"type": "text", "text": "hi there"}]
    assert body["usage"] == {"input_tokens": 3, "output_tokens": 5}
    # Alias -> upstream id resolution and body passthrough (no translation).
    assert FakeAnthropic.last_kwargs["model"] == _MODEL_ID
    assert FakeAnthropic.last_kwargs["max_tokens"] == 16
    assert FakeAnthropic.last_init["api_key"] == ANTHROPIC_VALUES["api_key"]


async def test_native_call_bills_one_event_with_native_token_counts(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A priced Anthropic model records exactly ONE usage event, billed on the
    # native usage block (input_tokens=3, output_tokens=5 from the fake).
    _patch(monkeypatch)
    key, team, admin = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id=_MODEL_ID,
        model_type="chat",
        input_cost_per_token=0.01,
        output_cost_per_token=0.01,
    )
    resp = await client.post("/v1/messages", json=_BODY, headers=_bearer(key))
    assert resp.status_code == HTTP_200_OK
    usage = await _team_usage(client, team, admin)
    assert len(usage) == 1
    (row,) = usage
    assert row["calls"] == 1
    assert row["prompt_tokens"] == 3  # native input_tokens
    assert row["completion_tokens"] == 5  # native output_tokens
    assert row["cost"] == pytest.approx(0.08)  # (3 + 5) * 0.01


async def test_over_budget_rejected_at_admit_and_not_dispatched(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # First call bills 0.08 and is admitted; the second sees committed spend
    # (0.08) already over the 0.05 cap and is rejected at admit -> 402, never
    # dispatched. Nothing new is billed: the ledger still shows one call.
    _patch(monkeypatch)
    key, team, admin = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id=_MODEL_ID,
        model_type="chat",
        input_cost_per_token=0.01,
        output_cost_per_token=0.01,
    )
    await client.put(
        f"/teams/{team}/budget",
        json={"limit_cost": 0.05, "window": "monthly"},
        headers=_bearer(admin),
    )
    first = await client.post("/v1/messages", json=_BODY, headers=_bearer(key))
    assert first.status_code == HTTP_200_OK
    blocked = await client.post("/v1/messages", json=_BODY, headers=_bearer(key))
    assert blocked.status_code == HTTP_402_PAYMENT_REQUIRED
    usage = await _team_usage(client, team, admin)
    assert len(usage) == 1
    assert usage[0]["calls"] == 1  # the blocked call never billed


async def test_non_anthropic_model_rejected_400(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # /v1/messages is the Anthropic wire shape; a non-Anthropic model behind it
    # is a misconfiguration -> ProviderMismatch (400), never dispatched or billed.
    _patch(monkeypatch)
    key, team, admin = await _setup_team(
        client, provider="openai", values=OPENAI_VALUES, model_type="chat"
    )
    resp = await client.post("/v1/messages", json=_BODY, headers=_bearer(key))
    assert resp.status_code == HTTP_400_BAD_REQUEST
    assert await _team_usage(client, team, admin) == []


async def test_upstream_error_maps_to_http_status_and_bills_nothing(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An upstream provider failure is normalized to a real HTTP status (not an
    # opaque 500); the reservation is released and nothing is billed.
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    _RaisingAnthropic.error = anthropic.APIStatusError(
        "boom", response=httpx.Response(503, request=request), body=None
    )
    monkeypatch.setattr(anthropic_adapter, "AsyncAnthropic", _RaisingAnthropic)
    key, team, admin = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id=_MODEL_ID,
        model_type="chat",
        input_cost_per_token=0.01,
        output_cost_per_token=0.01,
    )
    resp = await client.post("/v1/messages", json=_BODY, headers=_bearer(key))
    assert resp.status_code == HTTP_502_BAD_GATEWAY
    assert await _team_usage(client, team, admin) == []


# --- Native governance: reserved-kwarg rejection + output-token clamp -----------


@pytest.mark.parametrize(
    "bad_field", ["extra_headers", "extra_query", "extra_body", "timeout", "_x"]
)
async def test_native_rejects_sdk_control_kwargs_before_dispatch(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch, bad_field: str
) -> None:
    # ISSUE-001: the native body is splatted into `messages.create(**body)`, so a
    # reserved SDK control kwarg (extra_headers/extra_query/extra_body/timeout) — or
    # any leading-underscore key — would let a tenant override the vaulted
    # credential / inject outbound headers. They are rejected (400) centrally BEFORE
    # dispatch, so they never reach the SDK and nothing is billed.
    _patch(monkeypatch)
    key, team, admin = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id=_MODEL_ID,
        model_type="chat",
        input_cost_per_token=0.01,
        output_cost_per_token=0.01,
    )
    resp = await client.post(
        "/v1/messages", json={**_BODY, bad_field: {"X-Api-Key": "attacker"}}, headers=_bearer(key)
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST
    # Never dispatched: the fake SDK's create() was not called (kwargs stay empty).
    assert FakeAnthropic.last_kwargs == {}
    assert await _team_usage(client, team, admin) == []


async def test_native_output_ceiling_clamped_to_model_max(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ISSUE-003: the native surface must enforce the per-model output ceiling (H15)
    # like the OpenAI surface. With max_output_tokens=10, a body asking for 5000 is
    # clamped to 10 before dispatch.
    _patch(monkeypatch)
    key, _, _ = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id=_MODEL_ID,
        model_type="chat",
        max_output_tokens=10,
    )
    resp = await client.post(
        "/v1/messages", json={**_BODY, "max_tokens": 5000}, headers=_bearer(key)
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeAnthropic.last_kwargs["max_tokens"] == 10


async def test_native_output_ceiling_global_bound(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ISSUE-003: with no per-model ceiling, the global MAX_TOKENS still bounds the
    # native body's output field.
    _patch(monkeypatch)
    key, _, _ = await _setup_team(
        client,
        provider="anthropic",
        values=ANTHROPIC_VALUES,
        provider_model_id=_MODEL_ID,
        model_type="chat",
    )
    resp = await client.post(
        "/v1/messages", json={**_BODY, "max_tokens": 99_999}, headers=_bearer(key)
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeAnthropic.last_kwargs["max_tokens"] == MAX_TOKENS


# --- Native streaming settlement unit tests -------------------------------------
#
# Disconnect-safety (aclose propagation + shielded cancellation) is exercised at
# the CompletionService level with fake ports, mirroring the OpenAI-path
# tests/completions/test_stream_usage_fallback.py — the HTTP client can't
# interrupt an SSE response mid-flight. The native model is Provider.ANTHROPIC so
# it clears open_native_messages_stream's provider guard.

_TEAM_ID = uuid4()
_KEY_ID = uuid4()

# The raw Anthropic stream a non-tool `stream: true` request produces (matches the
# fake upstream client in tests/completions/conftest.py): input_tokens on
# message_start, cumulative output_tokens on message_delta.
_START_USAGE = {"input_tokens": 3, "output_tokens": 1}
_STREAM_EVENTS: list[dict[str, Any]] = [
    {"type": "message_start", "message": {"id": "msg-x", "usage": _START_USAGE}},
    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}},
    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": " there"}},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
    {"type": "message_stop"},
]

_STREAM_BODY: dict[str, Any] = {**_BODY, "stream": True}


def _anthropic_model() -> Model:
    return Model(
        id=uuid4(),
        team_id=_TEAM_ID,
        name="m",
        provider=Provider.ANTHROPIC,
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
        return {"api_key": "sk-ant-x"}  # pragma: allowlist secret


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


class _NativeStreamGateway:
    """Streams a fixed list of raw Anthropic events for the native path. When
    `blocked` is set it parks at the provider await after the last event — where a
    real client disconnect delivers its cancellation."""

    def __init__(self, events: list[dict[str, Any]], blocked: anyio.Event | None = None) -> None:
        self._events = events
        self._blocked = blocked

    async def astream_native_messages(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> AsyncIterator[dict[str, Any]]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[dict[str, Any]]:
        for event in self._events:
            yield event
        if self._blocked is not None:
            self._blocked.set()
            await anyio.sleep_forever()


def _native_service(
    gateway: Any, usage: _FakeUsage, traces: list[TraceRecord]
) -> CompletionService:
    return CompletionService(
        models=_FakeModels(_anthropic_model()),  # type: ignore[arg-type]
        credentials=_FakeCredentials(),  # type: ignore[arg-type]
        gateway=gateway,
        meter=UsageMeter(usage=usage, emit_trace=traces.append),  # type: ignore[arg-type]
    )


async def test_native_stream_tail_bills_once_with_native_tokens() -> None:
    # A fully-consumed native stream relays the raw Anthropic events unchanged
    # (Anthropic-shaped `type`s, never OpenAI `choices`) and bills exactly once at
    # the tail on the native input_tokens=3 / output_tokens=5.
    traces: list[TraceRecord] = []
    usage = _FakeUsage()
    service = _native_service(_NativeStreamGateway(list(_STREAM_EVENTS)), usage, traces)

    stream = await service.open_native_messages_stream(_TEAM_ID, _KEY_ID, dict(_STREAM_BODY))
    relayed = [event async for event in stream]

    assert [e["type"] for e in relayed] == [
        "message_start",
        "content_block_delta",
        "content_block_delta",
        "message_delta",
        "message_stop",
    ]
    assert all("choices" not in e for e in relayed)
    assert len(usage.events) == 1
    assert usage.events[0].prompt_tokens == 3
    assert usage.events[0].completion_tokens == 5
    assert [t.status for t in traces] == ["ok"]


async def test_native_stream_disconnect_mid_stream_still_settles() -> None:
    # Client disconnect mid-stream (aclose propagates through _rechain): settle once
    # with the native input_tokens already seen on message_start, billing what
    # streamed rather than leaking the reservation.
    traces: list[TraceRecord] = []
    usage = _FakeUsage()
    service = _native_service(_NativeStreamGateway(list(_STREAM_EVENTS)), usage, traces)

    stream = await service.open_native_messages_stream(_TEAM_ID, _KEY_ID, dict(_STREAM_BODY))
    consumed = 0
    async for _ in stream:
        consumed += 1
        if consumed == 2:  # message_start + first content_block_delta seen
            break
    assert isinstance(stream, AsyncGenerator)
    await stream.aclose()

    assert len(usage.events) == 1
    assert usage.events[0].prompt_tokens == 3  # native input_tokens from message_start
    # initial output_tokens from message_start; message_delta not reached
    assert usage.events[0].completion_tokens == 1
    assert [t.status for t in traces] == ["ok"]


async def test_native_stream_cancellation_mid_stream_still_settles() -> None:
    # A real disconnect cancels the streaming scope, raising CancelledError at the
    # provider await. The shielded settlement must still record the usage event
    # (H13 parity) — here all events streamed, so it bills input 3 / output 5.
    traces: list[TraceRecord] = []
    usage = _FakeUsage()
    blocked = anyio.Event()
    service = _native_service(_NativeStreamGateway(list(_STREAM_EVENTS), blocked), usage, traces)

    stream = await service.open_native_messages_stream(_TEAM_ID, _KEY_ID, dict(_STREAM_BODY))

    async def consume() -> None:
        async for _ in stream:
            pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(consume)
        await blocked.wait()  # all events streamed; consumer parked at provider await
        tg.cancel_scope.cancel()

    assert len(usage.events) == 1
    assert usage.events[0].prompt_tokens == 3
    assert usage.events[0].completion_tokens == 5
    assert [t.status for t in traces] == ["ok"]
