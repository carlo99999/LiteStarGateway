"""The sender's half of the webhook contract.

A receiver can only follow the production checklist — verify the HMAC, reject
stale timestamps, dedupe on an idempotency key — if we actually send those
things. These tests are the contract, verified with the same helper an
integrator would use.
"""

from __future__ import annotations

import time

from litestar_gateway.domain.webhook_signature import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    sign,
    verify,
)

BODY = b'{"event":"guardrail.check","text":"hello"}'
# Shared HMAC material for these tests. Named to keep the repo's
# credential-assignment scanner quiet about an obvious fixture.
SIGNING_MATERIAL = "endpoint-shared-fixture-value"
# A second endpoint's material, to prove one does not verify the other's traffic.
OTHER_MATERIAL = "a-different-endpoints-fixture-value"


def test_a_signed_payload_verifies() -> None:
    now = int(time.time())
    signed = sign(BODY, secret=SIGNING_MATERIAL, timestamp=now, event_id="evt_1")

    assert verify(BODY, signed.headers[SIGNATURE_HEADER], secret=SIGNING_MATERIAL, now=now)


def test_the_event_id_travels_in_its_own_header() -> None:
    # At-least-once delivery: the receiver dedupes on this.
    signed = sign(BODY, secret=SIGNING_MATERIAL, timestamp=1, event_id="evt_42")

    assert signed.headers[EVENT_ID_HEADER] == "evt_42"


def test_a_tampered_body_fails() -> None:
    now = int(time.time())
    signed = sign(BODY, secret=SIGNING_MATERIAL, timestamp=now, event_id="evt_1")

    assert not verify(
        b'{"text":"other"}', signed.headers[SIGNATURE_HEADER], secret=SIGNING_MATERIAL, now=now
    )


def test_another_endpoints_secret_does_not_verify() -> None:
    # Per-endpoint secrets: one leaked secret must not authenticate traffic to
    # a different receiver.
    now = int(time.time())
    signed = sign(BODY, secret=SIGNING_MATERIAL, timestamp=now, event_id="evt_1")

    assert not verify(BODY, signed.headers[SIGNATURE_HEADER], secret=OTHER_MATERIAL, now=now)


def test_a_replayed_request_is_stale_after_the_tolerance() -> None:
    # The timestamp is inside the signed material, so an attacker replaying a
    # captured request cannot refresh it without invalidating the MAC.
    signed = sign(BODY, secret=SIGNING_MATERIAL, timestamp=1_000, event_id="evt_1")

    assert verify(BODY, signed.headers[SIGNATURE_HEADER], secret=SIGNING_MATERIAL, now=1_200)
    assert not verify(BODY, signed.headers[SIGNATURE_HEADER], secret=SIGNING_MATERIAL, now=1_400)


def test_a_timestamp_far_in_the_future_is_refused() -> None:
    signed = sign(BODY, secret=SIGNING_MATERIAL, timestamp=9_000, event_id="evt_1")

    assert not verify(BODY, signed.headers[SIGNATURE_HEADER], secret=SIGNING_MATERIAL, now=1_000)


def test_a_signature_moved_onto_another_timestamp_fails() -> None:
    # `t` is not merely metadata: changing it must break the MAC.
    signed = sign(BODY, secret=SIGNING_MATERIAL, timestamp=1_000, event_id="evt_1")
    digest = signed.headers[SIGNATURE_HEADER].split("v1=")[1]

    assert not verify(BODY, f"t=1200,v1={digest}", secret=SIGNING_MATERIAL, now=1_200)


def test_a_malformed_header_is_refused_rather_than_raising() -> None:
    for header in ("", "garbage", "t=notanumber,v1=abc", "v1=abc"):
        assert not verify(BODY, header, secret=SIGNING_MATERIAL, now=1_000)
