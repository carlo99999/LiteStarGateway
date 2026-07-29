"""S2 — external webhook strategy.

The admin points the router at their own HTTP endpoint; the gateway does not
care what's behind it (a heuristic, an ML model, a random pick). Contract
(documented in docs/routing-webhook.md):

    POST <url>                          timeout: `timeout_ms` (default 2000)
    { "task": "<user text>", "system_prompt": "<or null>",
      "models": ["m1", "m2", ...], "metadata": { "estimated_tokens": 123 } }

    → 200 { "choice": 2 }               1-based index into "models",
                                        or { "choice": "m2" } by name

Anything else — non-2xx, timeout, malformed body, out-of-range index, unknown
name — raises, and the caller falls back to `default_model` per §4. Validation
is strict at this boundary: a sloppy webhook must never steer silently.

The URL must point at a public endpoint (SSRF guard, R6-H18): private,
loopback, link-local, multicast, reserved and unspecified targets are rejected
— literal IPs at config-save time, hostnames on every call after DNS
resolution — and redirects are never followed.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from litestar_gateway.application.egress import (
    _is_blocked,
    _literal_ip,
    resolve_approved_addresses,
)
from litestar_gateway.domain.routing import CandidateModel, RoutingContext, RoutingDecision
from litestar_gateway.domain.webhook_signature import sign

STRATEGY_ID = "webhook"
logger = logging.getLogger("litestar_gateway.routing.webhook")

DEFAULT_TIMEOUT_MS = 2000


def _client_factory(timeout_seconds: float) -> httpx.AsyncClient:
    """Module-level for test injection."""
    return httpx.AsyncClient(timeout=timeout_seconds)


async def post_to_approved_address(
    client: httpx.AsyncClient,
    url: httpx.URL,
    host: str,
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...],
    payload: dict[str, Any] | None,
    headers: dict[str, str],
    content: bytes | None = None,
) -> httpx.Response:
    """Connect to validated IPs while retaining the original HTTP/TLS
    identity (Host header + SNI = `host`, not the pinned IP). Shared by
    `WebhookStrategy`, the Plan 07 budget-alert webhook channel and the
    guardrail webhook provider.

    `content` sends pre-serialized bytes instead of letting httpx encode a
    dict. A signed payload has to be transmitted byte-for-byte as it was
    signed — re-encoding it here (different key order, different separators)
    would produce a body whose MAC no longer verifies."""
    for index, address in enumerate(addresses):
        pinned_url = url.copy_with(host=str(address))
        try:
            # The URL's host is an already validated IP, so the transport
            # cannot resolve the user-controlled hostname again. Host and
            # SNI remain the original hostname for virtual hosting and TLS
            # certificate validation. Redirects stay disabled.
            return await client.post(
                pinned_url,
                json=payload if content is None else None,
                content=content,
                headers=headers,
                follow_redirects=False,
                extensions={"sni_hostname": host},
            )
        except httpx.ConnectError, httpx.ConnectTimeout:
            if index == len(addresses) - 1:
                raise
    raise RuntimeError("validated address list is unexpectedly empty")  # pragma: no cover


class WebhookStrategy:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        url = config.get("url")
        # https only. This payload carries the user's prompt and system prompt,
        # and the SSRF guard already refuses loopback and private ranges — so a
        # plaintext target is by definition a public one, and sending prompts to
        # it in cleartext is never the intended configuration.
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError("webhook strategy requires an https 'url' in strategy_config")
        host = urlsplit(url).hostname
        if not host:
            raise ValueError("webhook 'url' has no host")
        literal = _literal_ip(host)
        if literal is not None and _is_blocked(literal):
            raise ValueError(
                f"webhook 'url' targets a private/loopback/link-local address ({host}); "
                "only public endpoints are allowed"
            )
        self._url = httpx.URL(url)
        self._host = host
        self._host_header = self._url.netloc.decode("ascii")
        self._bearer_token = config.get("bearer_token")
        # Per-endpoint HMAC key, from the router's own strategy_config: this one
        # can be per endpoint without a migration because strategy_config is
        # already free-form JSON.
        self._signing_secret = config.get("signing_secret")
        self._timeout_seconds = config.get("timeout_ms", DEFAULT_TIMEOUT_MS) / 1000

    async def select(
        self, ctx: RoutingContext, candidates: tuple[CandidateModel, ...]
    ) -> RoutingDecision:
        start = perf_counter()
        names = [candidate.model_name for candidate in candidates]
        # One id per call: this strategy is synchronous and never retried, so
        # there is no second delivery of the same decision to deduplicate —
        # the id is here so a receiver can correlate its logs with ours.
        event_id = str(uuid4())
        payload = {
            "event_id": event_id,
            "task": ctx.user_text,
            "system_prompt": ctx.system_prompt,
            "models": names,
            "metadata": {"estimated_tokens": ctx.estimated_input_tokens},
        }
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        auth_headers = (
            {"Authorization": f"Bearer {self._bearer_token}"} if self._bearer_token else {}
        )
        headers = {
            **auth_headers,
            **self._signature_headers(body, event_id=event_id),
            "Host": self._host_header,
            "Content-Type": "application/json",
        }
        addresses = await self._ensure_public_target()
        async with _client_factory(self._timeout_seconds) as client:
            response = await self._post_to_approved_address(
                client, addresses, None, headers, content=body
            )
        response.raise_for_status()
        chosen = self._parse_choice(response.json(), names)
        return RoutingDecision(
            model_name=chosen,
            strategy=STRATEGY_ID,
            tier=None,
            score=None,
            signals=(f"webhook chose {chosen}",),
            decision_ms=(perf_counter() - start) * 1000,
        )

    async def _post_to_approved_address(
        self,
        client: httpx.AsyncClient,
        addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...],
        payload: dict[str, Any] | None,
        headers: dict[str, str],
        content: bytes | None = None,
    ) -> httpx.Response:
        """Connect to validated IPs while retaining the original HTTP/TLS
        identity. Delegates to the module-level `post_to_approved_address`
        (shared with the Plan 07 budget-alert webhook channel)."""
        return await post_to_approved_address(
            client, self._url, self._host, addresses, payload, headers, content=content
        )

    def _signature_headers(self, body: bytes, *, event_id: str) -> dict[str, str]:
        """Sign when the router's config carries a secret; warn when it does not.

        A warning rather than a refusal because unsigned is the pre-existing
        behaviour and an existing router must keep working after an upgrade. But
        this payload contains the user's prompt, and a receiver with nothing to
        verify cannot tell our call from anyone else's who learned the URL."""
        if not self._signing_secret:
            logger.warning(
                "routing webhook has no 'signing_secret' in strategy_config: prompts are sent "
                "UNSIGNED, so the endpoint cannot verify they came from this gateway"
            )
            return {}
        signed = sign(
            body, secret=self._signing_secret, timestamp=int(time.time()), event_id=event_id
        )
        return signed.headers

    async def _ensure_public_target(
        self,
    ) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
        """SSRF guard (R6-H18); a blocked target raises, which the caller
        treats as any other strategy failure (fallback to default_model, §4).
        Delegates to the module-level `resolve_approved_addresses` (shared
        with the Plan 07 budget-alert webhook channel)."""
        return await resolve_approved_addresses(self._host)

    @staticmethod
    def _parse_choice(body: Any, names: list[str]) -> str:
        """Strict boundary validation: exactly {"choice": <1-based int | name>}."""
        if not isinstance(body, dict) or "choice" not in body:
            raise ValueError(f"webhook response missing 'choice': {body!r}")
        choice = body["choice"]
        # bool is an int subclass — reject it explicitly.
        if isinstance(choice, int) and not isinstance(choice, bool):
            if not 1 <= choice <= len(names):
                raise ValueError(f"webhook choice {choice} out of range 1..{len(names)}")
            return names[choice - 1]
        if isinstance(choice, str):
            if choice not in names:
                raise ValueError(f"webhook chose unknown model {choice!r}")
            return choice
        raise ValueError(f"webhook 'choice' must be a 1-based index or a model name: {choice!r}")
