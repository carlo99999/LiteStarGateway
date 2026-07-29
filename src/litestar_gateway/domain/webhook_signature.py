"""Signing what we send: HMAC + timestamp + event id.

The gateway is a webhook *sender*. The receiver's side of the contract —
"verify the HMAC on every request, reject stale timestamps, dedupe on the
idempotency key" — is only possible if we give them something to verify,
something to age, and something to dedupe on. This module produces all three.

The scheme is Stripe's, because it is the one most receivers already have code
for: a `t=<unix>,v1=<hex>` header, where the MAC covers `"{timestamp}.{body}"`
rather than the body alone. Binding the timestamp into the signed material is
what makes it a replay defence instead of a decoration — a captured request
cannot be re-sent later with a fresh timestamp without invalidating the MAC.

Deliveries are at-least-once (a retry after a timeout may duplicate a delivery
that in fact succeeded), so every payload carries a stable `event_id`: the same
logical event keeps its id across retries, which is exactly what a receiver
needs to discard the second copy.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

SIGNATURE_HEADER = "X-Gateway-Signature"
EVENT_ID_HEADER = "X-Gateway-Event-Id"

# How much clock skew a receiver should tolerate. Documented here because it is
# the number our own verification helper uses and the one the docs quote.
DEFAULT_TOLERANCE_SECONDS = 300


@dataclass(frozen=True)
class SignedPayload:
    """A body plus the headers that let the receiver trust it."""

    body: bytes
    headers: dict[str, str]


def sign(body: bytes, *, secret: str, timestamp: int, event_id: str) -> SignedPayload:
    """Sign `body` for delivery.

    `secret` is per-endpoint: one leaked secret must not authenticate traffic to
    a different receiver.
    """
    signature = _digest(body, secret=secret, timestamp=timestamp)
    return SignedPayload(
        body=body,
        headers={
            SIGNATURE_HEADER: f"t={timestamp},v1={signature}",
            EVENT_ID_HEADER: event_id,
        },
    )


def verify(
    body: bytes,
    header: str,
    *,
    secret: str,
    now: int,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    """The receiver's side, provided so our own tests — and anyone integrating —
    check against the same implementation the sender uses.

    Compared with `hmac.compare_digest`: a naive `==` on a hex string leaks how
    many leading characters matched through its timing, which is enough to
    forge a signature byte by byte.
    """
    parts = dict(piece.split("=", 1) for piece in header.split(",") if "=" in piece)
    try:
        timestamp = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs(now - timestamp) > tolerance_seconds:
        return False  # too old (replay) or too far in the future (bad clock)
    expected = _digest(body, secret=secret, timestamp=timestamp)
    return hmac.compare_digest(expected, parts.get("v1", ""))


def _digest(body: bytes, *, secret: str, timestamp: int) -> str:
    signed_material = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), signed_material, hashlib.sha256).hexdigest()
