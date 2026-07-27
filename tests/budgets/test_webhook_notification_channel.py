"""Plan 07 Phase 2 — webhook `NotificationChannel` adapter.

Reuses the exact SSRF-guarded egress primitives from
`application/routing/webhook.py` (the module-level `resolve_approved_addresses`
`post_to_approved_address` helpers, plus `_client_factory`/
`_resolve_host_addresses` for test injection) — these tests monkeypatch the
same module attributes `tests/routing/test_webhook_shadow.py` does, which is
what proves the alert channel goes through the identical deny-list/pinning
code path rather than a re-implementation of it."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

import litestar_gateway.application.routing.webhook as webhook_module
from litestar_gateway.domain.entities import BudgetWindow, PendingBudgetAlert
from litestar_gateway.infrastructure.notifications.webhook_channel import (
    WebhookNotificationChannel,
)


def _alert(**overrides) -> PendingBudgetAlert:
    defaults = dict(
        id=uuid4(),
        team_id=uuid4(),
        window=BudgetWindow.MONTHLY,
        period_start=datetime(2026, 7, 1, tzinfo=UTC),
        threshold=80,
        spend=85.0,
        limit_cost=100.0,
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return PendingBudgetAlert(**defaults)


def _mock_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    def factory(timeout_seconds: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout_seconds, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(webhook_module, "_client_factory", factory)


async def _public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


# ── unit: well-formed POST ───────────────────────────────────────────────────


async def test_send_posts_alert_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhook_module, "_resolve_host_addresses", _public_resolver)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200)

    _mock_transport(monkeypatch, handler)
    alert = _alert()
    channel = WebhookNotificationChannel("https://alerts.example/hook", bearer_token="tok")

    await channel.send(alert)

    assert seen["auth"] == "Bearer tok"
    assert seen["payload"] == {
        "team_id": str(alert.team_id),
        "window": "monthly",
        "period_start": alert.period_start.isoformat(),
        "threshold": 80,
        "spend": 85.0,
        "limit_cost": 100.0,
    }


async def test_send_raises_on_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhook_module, "_resolve_host_addresses", _public_resolver)
    _mock_transport(monkeypatch, lambda r: httpx.Response(500))
    channel = WebhookNotificationChannel("https://alerts.example/hook")
    with pytest.raises(httpx.HTTPStatusError):
        await channel.send(_alert())


def test_requires_http_url() -> None:
    for bad in ("ftp://x", "not-a-url", ""):
        with pytest.raises(ValueError):
            WebhookNotificationChannel(bad)


# ── regression: SSRF guard (R6-H18), byte-for-byte with the routing webhook ──


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://169.254.169.254/latest/meta-data",  # cloud metadata (link-local)
        "http://127.0.0.1:9/hook",  # loopback
        "http://10.0.0.5/hook",  # private 10/8
        "http://192.168.1.1/hook",  # private 192.168/16
        "http://[::1]/hook",  # IPv6 loopback
        "http://0.0.0.0/hook",  # unspecified
    ],
)
def test_rejects_blocked_literal_ip_at_config_time(bad_url: str) -> None:
    with pytest.raises(ValueError):
        WebhookNotificationChannel(bad_url)


async def test_rejects_hostname_resolving_to_private_at_send_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return httpx.Response(200)

    _mock_transport(monkeypatch, handler)

    async def private_resolver(host: str) -> list[str]:
        return ["10.1.2.3"]

    monkeypatch.setattr(webhook_module, "_resolve_host_addresses", private_resolver)
    channel = WebhookNotificationChannel("https://alerts.example/hook")
    with pytest.raises(ValueError):
        await channel.send(_alert())
    assert called == []  # blocked before any bytes leave the gateway


async def test_dns_rebinding_is_caught_by_per_call_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostname that resolved to a public IP at config time (channel
    construction does a literal-IP check only, not a DNS lookup) but a
    private IP at send time must still be rejected — the guard re-resolves
    and re-validates on every call."""
    _mock_transport(monkeypatch, lambda r: httpx.Response(200))

    async def rebinding_resolver(host: str) -> list[str]:
        return ["10.0.0.9"]  # "later" resolution: private

    monkeypatch.setattr(webhook_module, "_resolve_host_addresses", rebinding_resolver)
    # Construction succeeds: "alerts.example" is a hostname, not a literal IP,
    # so no DNS happens until send().
    channel = WebhookNotificationChannel("https://alerts.example/hook")
    with pytest.raises(ValueError):
        await channel.send(_alert())


async def test_public_hostname_passes_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhook_module, "_resolve_host_addresses", _public_resolver)
    _mock_transport(monkeypatch, lambda r: httpx.Response(200))
    await WebhookNotificationChannel("https://alerts.example/hook").send(_alert())


async def test_connects_to_pinned_ip_retaining_host_and_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    async def resolver(host: str) -> list[str]:
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(webhook_module, "_resolve_host_addresses", resolver)
    _mock_transport(monkeypatch, handler)

    await WebhookNotificationChannel("https://alerts.example:8443/hook").send(_alert())

    assert len(requests) == 1
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["Host"] == "alerts.example:8443"
    assert requests[0].extensions["sni_hostname"] == "alerts.example"
