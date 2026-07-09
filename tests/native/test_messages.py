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

import anthropic
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
    HTTP_501_NOT_IMPLEMENTED,
    HTTP_502_BAD_GATEWAY,
)
from litestar.testing import AsyncTestClient

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


async def test_streaming_still_501(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Streaming native Messages is a later slice; a valid streamed request clears
    # the guards and hits the not-yet-implemented branch -> 501, bills nothing.
    _patch(monkeypatch)
    key, team, admin = await _setup_team(
        client, provider="anthropic", values=ANTHROPIC_VALUES, model_type="chat"
    )
    resp = await client.post("/v1/messages", json={**_BODY, "stream": True}, headers=_bearer(key))
    assert resp.status_code == HTTP_501_NOT_IMPLEMENTED
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
