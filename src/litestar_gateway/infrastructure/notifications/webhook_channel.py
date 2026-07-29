"""Webhook `NotificationChannel` adapter (Plan 07 Phase 2, design doc §4).

Reuses the SSRF-guarded egress from `application/routing/webhook.py`
verbatim: the literal + resolved-IP deny-list (`_is_blocked`/`_literal_ip`),
the per-call DNS-rebinding re-check (`resolve_approved_addresses`), and the
IP-pinned POST that retains the original Host header and TLS SNI
(`post_to_approved_address`). Nothing here re-implements any part of that
guard — a budget-alert webhook targeting a private/loopback/link-local
address is rejected the exact same way a routing webhook is."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from litestar_gateway.application.routing import webhook as webhook_module
from litestar_gateway.application.routing.webhook import (
    DEFAULT_TIMEOUT_MS,
    _is_blocked,
    _literal_ip,
    post_to_approved_address,
    resolve_approved_addresses,
)
from litestar_gateway.domain.entities import PendingBudgetAlert


class WebhookNotificationChannel:
    """Delivers a fired budget-threshold alert as a single POST to one
    operator-configured webhook URL. Phase 2 wires this as a single
    platform-wide target sourced from `Settings.budget_alert_webhook_url`;
    per-team targets are Phase 3's config-surface work on the budget
    endpoints (`plans/07-budget-alerts.md`)."""

    def __init__(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("budget alert webhook requires an http(s) url")
        host = urlsplit(url).hostname
        if not host:
            raise ValueError("budget alert webhook url has no host")
        literal = _literal_ip(host)
        if literal is not None and _is_blocked(literal):
            raise ValueError(
                f"budget alert webhook url targets a private/loopback/link-local "
                f"address ({host}); only public endpoints are allowed"
            )
        self._url = httpx.URL(url)
        self._host = host
        self._host_header = self._url.netloc.decode("ascii")
        self._bearer_token = bearer_token
        self._timeout_seconds = timeout_ms / 1000

    async def send(self, alert: PendingBudgetAlert) -> None:
        payload: dict[str, Any] = {
            "team_id": str(alert.team_id),
            "window": alert.window.value,
            "period_start": alert.period_start.isoformat(),
            "threshold": alert.threshold,
            # JSON numbers, like every other outbound payload: `Decimal` is
            # authoritative in the domain and at rest, never on the wire — and
            # `json.dumps` cannot serialize it at all.
            "spend": float(alert.spend),
            "limit_cost": float(alert.limit_cost),
        }
        auth_headers = (
            {"Authorization": f"Bearer {self._bearer_token}"} if self._bearer_token else {}
        )
        headers = {**auth_headers, "Host": self._host_header}
        # Re-checked on every send (DNS-rebinding guard), not cached from
        # construction — see `resolve_approved_addresses`.
        addresses = await resolve_approved_addresses(self._host)
        # Looked up on the module (not imported by value) so tests that
        # monkeypatch `webhook_module._client_factory` — the same fixture
        # `test_webhook_shadow.py` uses for the routing webhook — take effect
        # here too.
        async with webhook_module._client_factory(self._timeout_seconds) as client:
            response = await post_to_approved_address(
                client, self._url, self._host, addresses, payload, headers
            )
        response.raise_for_status()
