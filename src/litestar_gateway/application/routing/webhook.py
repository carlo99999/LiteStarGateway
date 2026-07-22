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

import asyncio
import ipaddress
import socket
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

import httpx

from litestar_gateway.domain.routing import CandidateModel, RoutingContext, RoutingDecision

STRATEGY_ID = "webhook"
DEFAULT_TIMEOUT_MS = 2000


def _client_factory(timeout_seconds: float) -> httpx.AsyncClient:
    """Module-level for test injection."""
    return httpx.AsyncClient(timeout=timeout_seconds)


async def _resolve_host_addresses(host: str) -> list[str]:
    """Async DNS resolution (module-level for test injection)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """SSRF deny-list: anything that isn't a plain public unicast address."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


class WebhookStrategy:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        url = config.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("webhook strategy requires an http(s) 'url' in strategy_config")
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
        self._timeout_seconds = config.get("timeout_ms", DEFAULT_TIMEOUT_MS) / 1000

    async def select(
        self, ctx: RoutingContext, candidates: tuple[CandidateModel, ...]
    ) -> RoutingDecision:
        start = perf_counter()
        names = [candidate.model_name for candidate in candidates]
        payload = {
            "task": ctx.user_text,
            "system_prompt": ctx.system_prompt,
            "models": names,
            "metadata": {"estimated_tokens": ctx.estimated_input_tokens},
        }
        auth_headers = (
            {"Authorization": f"Bearer {self._bearer_token}"} if self._bearer_token else {}
        )
        headers = {**auth_headers, "Host": self._host_header}
        addresses = await self._ensure_public_target()
        async with _client_factory(self._timeout_seconds) as client:
            response = await self._post_to_approved_address(client, addresses, payload, headers)
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
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> httpx.Response:
        """Connect to validated IPs while retaining the original HTTP/TLS identity."""
        for index, address in enumerate(addresses):
            pinned_url = self._url.copy_with(host=str(address))
            try:
                # The URL's host is an already validated IP, so the transport
                # cannot resolve the user-controlled hostname again. Host and
                # SNI remain the original hostname for virtual hosting and TLS
                # certificate validation. Redirects stay disabled.
                return await client.post(
                    pinned_url,
                    json=payload,
                    headers=headers,
                    follow_redirects=False,
                    extensions={"sni_hostname": self._host},
                )
            except httpx.ConnectError, httpx.ConnectTimeout:
                if index == len(addresses) - 1:
                    raise
        raise RuntimeError(
            "validated webhook address list is unexpectedly empty"
        )  # pragma: no cover

    async def _ensure_public_target(
        self,
    ) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
        """SSRF guard (R6-H18), re-checked on every call to resist DNS
        rebinding between config-save and use: every address the host resolves
        to must be public. A blocked target raises, which the caller treats as
        any other strategy failure (fallback to default_model, §4)."""
        literal = _literal_ip(self._host)
        addresses = (literal,) if literal is not None else None
        if addresses is None:
            resolved = await _resolve_host_addresses(self._host)
            addresses = tuple(ipaddress.ip_address(address) for address in resolved)
        if not addresses:
            raise ValueError(f"webhook host {self._host!r} did not resolve to any address")
        for address in addresses:
            if _is_blocked(address):
                raise ValueError(
                    f"webhook host {self._host!r} resolves to blocked address {address}; "
                    "only public endpoints are allowed"
                )
        return addresses

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
