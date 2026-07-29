"""Guardrail provider that asks an operator-run HTTP endpoint for a verdict.

This is the one provider that sends the user's prompt off the gateway, so it
carries the obligations of a webhook *sender*, not just of an HTTP client:

- **signed**: HMAC-SHA256 over `"{timestamp}.{body}"` in `X-Gateway-Signature`,
  so the receiver can verify the call came from this gateway and was not
  replayed. Per-endpoint secret;
- **identified**: a stable `event_id` header and field, so a receiver that sees
  the same check twice (our own retry, a proxy, a network duplicate) can tell;
- **bounded**: a hard, short time budget. A guardrail sits *inside* the request
  path, so the endpoint's latency is the caller's latency — the usual "5 seconds
  and then the sender gives up" applies here as our own deadline, defaulted well
  below it;
- **guarded**: HTTPS to a public address only, resolved and re-checked on every
  call, with the connection pinned to the validated IP. The same SSRF machinery
  the routing webhook and the budget-alert channel use, not a second copy.

The response contract is deliberately small and strictly validated: an endpoint
that answers with anything else is a *provider failure*, resolved by the
configured fail policy, never a silent allow.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from litestar_gateway.application.routing.webhook import (
    _is_blocked,
    _literal_ip,
    post_to_approved_address,
    resolve_approved_addresses,
)
from litestar_gateway.domain.guardrails import (
    Decision,
    Direction,
    GuardrailPayload,
    GuardrailVerdict,
)
from litestar_gateway.domain.webhook_signature import sign

# Well under the 5 s a webhook sender conventionally allows: this call is on the
# request path, so every millisecond is the caller's.
DEFAULT_TIMEOUT_MS = 2000

_DECISIONS = {d.value: d for d in Decision}


class WebhookGuardrail:
    """One configured endpoint, asked for a verdict on a payload."""

    def __init__(
        self,
        url: str,
        *,
        secret: str,
        name: str = "webhook",
        directions: tuple[Direction, ...] = (Direction.REQUEST,),
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        client_factory: Any = None,
    ) -> None:
        if not url.startswith("https://"):
            # The payload is the user's prompt; plaintext is not an option, and
            # unlike a notification there is a verdict coming back to trust.
            raise ValueError("guardrail webhook requires an https:// url")
        host = urlsplit(url).hostname
        if not host:
            raise ValueError("guardrail webhook url has no host")
        literal = _literal_ip(host)
        if literal is not None and _is_blocked(literal):
            raise ValueError(
                f"guardrail webhook url targets a private/loopback/link-local address ({host}); "
                "only public endpoints are allowed"
            )
        if not secret:
            raise ValueError("guardrail webhook requires a signing secret")
        self.name = name
        self._url = httpx.URL(url)
        self._host = host
        self._host_header = self._url.netloc.decode("ascii")
        self._secret = secret
        self._directions = directions
        self._timeout_seconds = timeout_ms / 1000
        self._client_factory = client_factory or (
            lambda seconds: httpx.AsyncClient(timeout=seconds)
        )

    def supports(self, direction: Direction) -> bool:
        return direction in self._directions

    async def check(self, payload: GuardrailPayload) -> GuardrailVerdict:
        event_id = str(uuid4())
        body = json.dumps(
            {
                "event_id": event_id,
                "event": "guardrail.check",
                "direction": payload.direction.value,
                "text": payload.text,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        signed = sign(body, secret=self._secret, timestamp=int(time.time()), event_id=event_id)
        headers = {
            **signed.headers,
            "Host": self._host_header,
            "Content-Type": "application/json",
        }

        addresses = await resolve_approved_addresses(self._host)
        async with self._client_factory(self._timeout_seconds) as client:
            response = await post_to_approved_address(
                client, self._url, self._host, addresses, None, headers, content=signed.body
            )
        response.raise_for_status()
        return self._parse(response.json())

    def _parse(self, body: Any) -> GuardrailVerdict:
        """Strict: an endpoint that answers off-contract has not answered.

        Raising here surfaces as a provider failure and is resolved by the
        configured fail policy — a malformed body must never be read as an
        allow, which is the quiet way a guardrail stops guarding.
        """
        if not isinstance(body, dict):
            raise ValueError("guardrail webhook response must be a JSON object")
        raw_decision = body.get("decision")
        decision = _DECISIONS.get(raw_decision) if isinstance(raw_decision, str) else None
        if decision is None:
            raise ValueError(f"unknown guardrail decision {raw_decision!r}")
        categories = tuple(str(c) for c in body.get("categories", ()) if isinstance(c, str))
        redacted = body.get("redacted_text")
        if decision is Decision.REDACT and not isinstance(redacted, str):
            raise ValueError("a redact verdict must carry redacted_text")
        return GuardrailVerdict(
            decision=decision,
            provider=self.name,
            categories=categories,
            counts={c: 1 for c in categories},
            redacted_text=redacted if isinstance(redacted, str) else None,
        )
