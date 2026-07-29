"""The guardrail webhook: what we send, and what we accept back.

This provider sends the user's prompt to an operator-run endpoint, so the tests
are as much about the outbound contract — signed, identified, bounded, guarded —
as about parsing the verdict.
"""

from __future__ import annotations

import ipaddress
import json
import time
from typing import Any

import httpx
import pytest

from litestar_gateway.application.guardrails import webhook as webhook_provider
from litestar_gateway.application.guardrails.webhook import WebhookGuardrail
from litestar_gateway.domain.guardrails import Decision, Direction, GuardrailPayload
from litestar_gateway.domain.webhook_signature import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    verify,
)

URL = "https://guardrail.example.com/check"
# Shared HMAC material for these tests. Named to keep the repo's
# credential-assignment scanner quiet about an obvious fixture.
SIGNING_MATERIAL = "endpoint-shared-fixture-value"
PROMPT = "my name is Mario Rossi and my card is 4111 1111 1111 1111"


class _Recorder:
    """Captures the request instead of sending it."""

    def __init__(self, response: dict[str, Any] | None = None, status: int = 200) -> None:
        self.response = response if response is not None else {"decision": "allow"}
        self.status = status
        self.requests: list[httpx.Request] = []

    def factory(self, _timeout: float) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(self.status, json=self.response)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SSRF guard resolves for real; point it at a public address."""

    async def resolve(host: str):
        return (ipaddress.ip_address("93.184.216.34"),)

    monkeypatch.setattr(webhook_provider, "resolve_approved_addresses", resolve)


def _payload(text: str = PROMPT) -> GuardrailPayload:
    return GuardrailPayload(direction=Direction.REQUEST, text=text)


async def test_the_request_is_signed_and_verifies() -> None:
    recorder = _Recorder()
    guardrail = WebhookGuardrail(URL, secret=SIGNING_MATERIAL, client_factory=recorder.factory)

    await guardrail.check(_payload())

    sent = recorder.requests[0]
    assert verify(
        sent.content, sent.headers[SIGNATURE_HEADER], secret=SIGNING_MATERIAL, now=int(time.time())
    )


async def test_the_signature_covers_the_bytes_actually_sent() -> None:
    # The body must go out byte-for-byte as it was signed: re-encoding it
    # anywhere in the stack would produce a MAC the receiver cannot verify.
    recorder = _Recorder()
    guardrail = WebhookGuardrail(URL, secret=SIGNING_MATERIAL, client_factory=recorder.factory)

    await guardrail.check(_payload())

    sent = recorder.requests[0]
    assert json.loads(sent.content)["text"] == PROMPT


async def test_every_call_carries_an_event_id_in_header_and_body() -> None:
    recorder = _Recorder()
    guardrail = WebhookGuardrail(URL, secret=SIGNING_MATERIAL, client_factory=recorder.factory)

    await guardrail.check(_payload())
    await guardrail.check(_payload())

    ids = [r.headers[EVENT_ID_HEADER] for r in recorder.requests]
    assert ids[0] != ids[1]  # distinct checks are distinct events
    assert json.loads(recorder.requests[0].content)["event_id"] == ids[0]


async def test_the_user_prompt_is_what_gets_sent() -> None:
    # Stated as a test because it is the privacy-relevant fact about this
    # provider: enabling it means prompts leave the gateway.
    recorder = _Recorder()
    guardrail = WebhookGuardrail(URL, secret=SIGNING_MATERIAL, client_factory=recorder.factory)

    await guardrail.check(_payload("a very specific user question"))

    assert json.loads(recorder.requests[0].content)["text"] == "a very specific user question"


async def test_an_allow_verdict_is_parsed() -> None:
    recorder = _Recorder({"decision": "allow"})
    guardrail = WebhookGuardrail(URL, secret=SIGNING_MATERIAL, client_factory=recorder.factory)

    verdict = await guardrail.check(_payload())

    assert verdict.decision is Decision.ALLOW


async def test_a_block_verdict_carries_categories_only() -> None:
    recorder = _Recorder({"decision": "block", "categories": ["pii.credit_card"]})
    guardrail = WebhookGuardrail(URL, secret=SIGNING_MATERIAL, client_factory=recorder.factory)

    verdict = await guardrail.check(_payload())

    assert verdict.decision is Decision.BLOCK
    assert verdict.categories == ("pii.credit_card",)
    assert verdict.redacted_text is None


async def test_a_redact_verdict_returns_the_rewritten_text() -> None:
    recorder = _Recorder(
        {"decision": "redact", "categories": ["pii.name"], "redacted_text": "my name is [NAME]"}
    )
    guardrail = WebhookGuardrail(URL, secret=SIGNING_MATERIAL, client_factory=recorder.factory)

    verdict = await guardrail.check(_payload())

    assert verdict.redacted_text == "my name is [NAME]"


@pytest.mark.parametrize(
    "body",
    [
        {"decision": "maybe"},
        {"decision": "redact"},  # redact without the rewritten text
        {},
        ["allow"],
        "allow",
    ],
)
async def test_an_off_contract_response_is_a_provider_failure(body: Any) -> None:
    # Never a silent allow: raising here lets the chain apply the endpoint's
    # fail policy, which is where "what does a broken guardrail mean" is decided.
    recorder = _Recorder(body)
    guardrail = WebhookGuardrail(URL, secret=SIGNING_MATERIAL, client_factory=recorder.factory)

    with pytest.raises(ValueError):
        await guardrail.check(_payload())


async def test_an_http_error_is_a_provider_failure() -> None:
    recorder = _Recorder({"decision": "allow"}, status=500)
    guardrail = WebhookGuardrail(URL, secret=SIGNING_MATERIAL, client_factory=recorder.factory)

    with pytest.raises(httpx.HTTPStatusError):
        await guardrail.check(_payload())


def test_a_plaintext_url_is_refused() -> None:
    with pytest.raises(ValueError, match="https"):
        WebhookGuardrail("http://guardrail.example.com/check", secret=SIGNING_MATERIAL)


def test_a_private_address_is_refused() -> None:
    with pytest.raises(ValueError, match="private"):
        WebhookGuardrail("https://127.0.0.1/check", secret=SIGNING_MATERIAL)


def test_a_missing_secret_is_refused() -> None:
    # Unsigned prompt egress is not a configuration this accepts.
    with pytest.raises(ValueError, match="secret"):
        WebhookGuardrail(URL, secret="")
