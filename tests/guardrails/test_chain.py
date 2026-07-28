"""The guardrail chain runner: how independent verdicts combine.

The combination rules are the whole contract — a provider is easy, deciding what
three of them together mean is not.
"""

from __future__ import annotations

import asyncio

import pytest

from litestar_gateway.application.guardrails.service import ChainedProvider, run_chain
from litestar_gateway.domain.exceptions import GuardrailBlocked
from litestar_gateway.domain.guardrails import (
    Decision,
    Direction,
    FailPolicy,
    GuardrailPayload,
    GuardrailVerdict,
)


class _Provider:
    """A provider that returns a scripted verdict, optionally after a delay or
    by raising."""

    def __init__(
        self,
        name: str,
        verdict: GuardrailVerdict | None = None,
        *,
        directions: tuple[Direction, ...] = (Direction.REQUEST, Direction.RESPONSE),
        raises: Exception | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.name = name
        self._verdict = verdict or GuardrailVerdict(decision=Decision.ALLOW, provider=name)
        self._directions = directions
        self._raises = raises
        self._delay_s = delay_s
        self.calls = 0

    def supports(self, direction: Direction) -> bool:
        return direction in self._directions

    async def check(self, payload: GuardrailPayload) -> GuardrailVerdict:
        self.calls += 1
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._raises is not None:
            raise self._raises
        return self._verdict


def _payload(text: str = "hello") -> GuardrailPayload:
    return GuardrailPayload(direction=Direction.REQUEST, text=text)


def _redactor(name: str, text: str) -> _Provider:
    return _Provider(
        name,
        GuardrailVerdict(
            decision=Decision.REDACT,
            provider=name,
            categories=("pii.email",),
            counts={"pii.email": 1},
            redacted_text=text,
        ),
    )


async def test_an_empty_chain_returns_the_payload_untouched() -> None:
    outcome = await run_chain((), _payload("hello"))

    assert outcome.text == "hello"
    assert outcome.verdicts == ()


async def test_a_chain_of_allows_changes_nothing() -> None:
    outcome = await run_chain(
        (ChainedProvider(_Provider("a")), ChainedProvider(_Provider("b"))), _payload("hello")
    )

    assert outcome.text == "hello"
    assert not outcome.redacted


async def test_a_single_block_refuses_the_request() -> None:
    blocker = _Provider(
        "moderation",
        GuardrailVerdict(
            decision=Decision.BLOCK, provider="moderation", categories=("moderation.violence",)
        ),
    )

    with pytest.raises(GuardrailBlocked, match="moderation"):
        await run_chain((ChainedProvider(_Provider("a")), ChainedProvider(blocker)), _payload())


async def test_a_block_wins_over_a_redact() -> None:
    # Severity, not order: the redactor is first in the chain and still loses.
    with pytest.raises(GuardrailBlocked):
        await run_chain(
            (
                ChainedProvider(_redactor("pii", "[REDACTED]")),
                ChainedProvider(
                    _Provider("mod", GuardrailVerdict(decision=Decision.BLOCK, provider="mod"))
                ),
            ),
            _payload(),
        )


async def test_the_block_message_never_carries_the_matched_content() -> None:
    sensitive_text = "please charge card 4111111111111111"
    blocker = _Provider(
        "pii",
        GuardrailVerdict(
            decision=Decision.BLOCK,
            provider="pii",
            categories=("pii.credit_card",),
            counts={"pii.credit_card": 1},
        ),
    )

    with pytest.raises(GuardrailBlocked) as raised:
        await run_chain((ChainedProvider(blocker),), _payload(sensitive_text))

    assert "4111111111111111" not in str(raised.value)
    assert "pii.credit_card" in str(raised.value)


async def test_redactions_compose_in_chain_order() -> None:
    # Two redactors, deterministic result: the configured order decides, not
    # whichever coroutine finished first.
    slow = _redactor("slow", "first-pass")
    fast = _redactor("fast", "second-pass")
    slow._delay_s = 0.01  # noqa: SLF001 - the point is that it still applies first

    outcome = await run_chain((ChainedProvider(slow), ChainedProvider(fast)), _payload("raw"))

    assert outcome.text == "second-pass"
    assert outcome.redacted


async def test_a_provider_is_not_asked_about_a_direction_it_does_not_support() -> None:
    response_only = _Provider("resp", directions=(Direction.RESPONSE,))

    await run_chain((ChainedProvider(response_only),), _payload())

    assert response_only.calls == 0


async def test_a_failing_provider_with_an_open_policy_lets_the_request_through() -> None:
    broken = _Provider("flaky", raises=RuntimeError("endpoint down"))

    outcome = await run_chain((ChainedProvider(broken, fail=FailPolicy.OPEN),), _payload("hello"))

    assert outcome.text == "hello"


async def test_a_failing_provider_with_a_closed_policy_refuses_it() -> None:
    # A control that could not be evaluated has not passed.
    broken = _Provider("compliance", raises=RuntimeError("endpoint down"))

    with pytest.raises(GuardrailBlocked, match="unavailable"):
        await run_chain((ChainedProvider(broken, fail=FailPolicy.CLOSED),), _payload())


async def test_one_provider_failing_does_not_hide_another_s_verdict() -> None:
    broken = _Provider("flaky", raises=RuntimeError("down"))
    redactor = _redactor("pii", "clean")

    outcome = await run_chain(
        (ChainedProvider(broken, fail=FailPolicy.OPEN), ChainedProvider(redactor)), _payload()
    )

    assert outcome.text == "clean"


async def test_providers_run_concurrently() -> None:
    # Independent checks: three 50 ms providers must not cost 150 ms.
    slow = [ChainedProvider(_Provider(f"p{i}", delay_s=0.05)) for i in range(3)]

    started = asyncio.get_running_loop().time()
    await run_chain(tuple(slow), _payload())

    assert asyncio.get_running_loop().time() - started < 0.12
