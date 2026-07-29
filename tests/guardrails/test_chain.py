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


class _MaskingProvider:
    """A provider whose verdict depends on the text it is handed.

    A redactor returning a fixed string cannot distinguish a pipeline from a
    race: last-writer-wins and proper composition produce the same answer. This
    one rewrites `needle` into `replacement`, so it only acts when the text it
    sees actually contains the needle — which is what makes the ordering
    observable.
    """

    def __init__(self, name: str, needle: str, replacement: str, *, blocks: bool = False) -> None:
        self.name = name
        self._needle = needle
        self._replacement = replacement
        self._blocks = blocks
        self.calls = 0
        self.delay_s = 0.0

    def supports(self, direction: Direction) -> bool:
        return True

    async def check(self, payload: GuardrailPayload) -> GuardrailVerdict:
        self.calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self._needle not in payload.text:
            return GuardrailVerdict(decision=Decision.ALLOW, provider=self.name)
        if self._blocks:
            return GuardrailVerdict(
                decision=Decision.BLOCK,
                provider=self.name,
                categories=(f"pii.{self.name}",),
            )
        return GuardrailVerdict(
            decision=Decision.REDACT,
            provider=self.name,
            categories=(f"pii.{self.name}",),
            counts={f"pii.{self.name}": 1},
            redacted_text=payload.text.replace(self._needle, self._replacement),
        )


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
    # `x` -> `y` -> `z` only reaches `z` if the second redactor saw the first
    # one's output. Fixed-string redactors cannot tell a pipeline from a race —
    # whichever wrote last wins either way — so both here depend on their input.
    first = _MaskingProvider("first", "x", "y")
    second = _MaskingProvider("second", "y", "z")
    first.delay_s = 0.01  # finishing last must not mean applying last

    outcome = await run_chain((ChainedProvider(first), ChainedProvider(second)), _payload("x"))

    assert outcome.text == "z"
    assert outcome.redacted


async def test_two_redactors_do_not_undo_each_other() -> None:
    # The leak: both ran on the original, so the surviving text was whichever
    # rewrite landed last — and the other provider's redaction was restored.
    emails = _MaskingProvider("email", "user@example.test", "[EMAIL]")
    cards = _MaskingProvider("card", "4111111111111111", "[CARD]")

    outcome = await run_chain(
        (ChainedProvider(emails), ChainedProvider(cards)),
        _payload("mail user@example.test card 4111111111111111"),
    )

    assert outcome.text == "mail [EMAIL] card [CARD]"
    assert "user@example.test" not in outcome.text
    assert "4111111111111111" not in outcome.text
    # Both verdicts still reach the audit row, not just the last one.
    assert {v.provider for v in outcome.verdicts if v.decision is Decision.REDACT} == {
        "email",
        "card",
    }


async def test_one_redactor_is_asked_exactly_once() -> None:
    # The common case must keep the concurrent single-pass cost: composing is
    # only needed when a second redactor actually fires.
    only = _MaskingProvider("only", "secret", "[X]")
    quiet = _MaskingProvider("quiet", "absent", "[Y]")

    outcome = await run_chain(
        (ChainedProvider(only), ChainedProvider(quiet)), _payload("a secret here")
    )

    assert outcome.text == "a [X] here"
    assert only.calls == 1
    assert quiet.calls == 1


async def test_a_block_found_while_composing_still_wins() -> None:
    # Re-reading composed text can surface what the original hid; a refusal
    # discovered there is still a refusal.
    masker = _MaskingProvider("mask", "raw", "tripwire")
    late_blocker = _MaskingProvider("late", "tripwire", "never", blocks=True)

    with pytest.raises(GuardrailBlocked, match="late"):
        await run_chain(
            (ChainedProvider(masker), ChainedProvider(late_blocker)), _payload("raw and more")
        )


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


async def test_providers_run_one_at_a_time_in_chain_order() -> None:
    # This replaces a test asserting the opposite — three 50 ms providers
    # finishing in under 120 ms, i.e. concurrently. That concurrency is exactly
    # what broke composition (ISSUE-039): a redactor cannot see the previous
    # rewrite if both ran on the original at once. The property is gone on
    # purpose, and the chain now pays the sum of its providers' times.
    #
    # Pinned by observation rather than by a stopwatch: each provider records
    # entering and leaving, and yields to the loop in between, so an
    # interleaving would show up as out-of-order events instead of as a flaky
    # duration.
    events: list[str] = []

    class _Recording:
        def __init__(self, name: str) -> None:
            self.name = name

        def supports(self, direction: Direction) -> bool:
            return True

        async def check(self, payload: GuardrailPayload) -> GuardrailVerdict:
            events.append(f"{self.name}:in")
            await asyncio.sleep(0)  # a concurrent runner would interleave here
            events.append(f"{self.name}:out")
            return GuardrailVerdict(decision=Decision.ALLOW, provider=self.name)

    await run_chain(
        (ChainedProvider(_Recording("a")), ChainedProvider(_Recording("b"))), _payload()
    )

    assert events == ["a:in", "a:out", "b:in", "b:out"]
