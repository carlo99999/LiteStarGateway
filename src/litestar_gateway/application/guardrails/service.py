"""The guardrail chain: run every applicable provider, combine their verdicts.

Combination rules, in order:

1. **any BLOCK wins.** A single provider refusing is enough; the others'
   opinions cannot overturn it.
2. **REDACTs compose.** Each redacting provider rewrites the text and the next
   one sees the rewritten version, so a chain of redactors is a pipeline rather
   than a race. Order is the configured order — deterministic, because two
   redactors can otherwise produce different text depending on who finished
   first.
3. **ALLOW is the identity.** A chain of allows returns the payload untouched.

Rule 2 is why the chain runs **sequentially**, one provider at a time, each
handed what the previous one left behind. Providers used to run concurrently on
the original text and the rewrites were merged afterwards, which cannot compose:
with two redactors the surviving text was whichever rewrite landed last, and the
other's redaction was silently restored (ISSUE-039). "The next one sees the
rewritten version" is only true if the next one runs after it.

Sequencing also means a provider is asked exactly once, a refusal short-circuits
the rest of the chain, and a check can act on what an earlier redaction exposed.
The cost is latency: a chain of N providers pays the sum of their times rather
than the slowest. Chains are short and every provider is time-bounded, so this
is the right side of the trade — a redactor that gets undone is not a slow
control, it is a broken one. Restoring concurrency for the checks that never
redact needs the Protocol to declare that up front, which it does not today.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from litestar_gateway.domain.exceptions import GuardrailBlocked
from litestar_gateway.domain.guardrails import (
    Decision,
    FailPolicy,
    GuardrailPayload,
    GuardrailProvider,
    GuardrailVerdict,
)

logger = logging.getLogger("litestar_gateway.guardrails")


@dataclass(frozen=True)
class ChainedProvider:
    """A provider plus the policy that decides what its own failure means."""

    provider: GuardrailProvider
    fail: FailPolicy = FailPolicy.OPEN


@dataclass(frozen=True)
class ChainOutcome:
    """What the chain decided: the (possibly rewritten) text, and every verdict
    that contributed — the audit row is built from these, and it contains
    categories and counts only."""

    text: str
    verdicts: tuple[GuardrailVerdict, ...] = ()

    @property
    def redacted(self) -> bool:
        return any(v.decision is Decision.REDACT for v in self.verdicts)


async def run_chain(
    chain: tuple[ChainedProvider, ...],
    payload: GuardrailPayload,
) -> ChainOutcome:
    """Run the chain for one direction and combine the verdicts.

    Raises `GuardrailBlocked` as soon as the combination says BLOCK — including
    when a `CLOSED`-policy provider could not be evaluated, because a control
    that did not run has not passed.
    """
    applicable = [c for c in chain if c.provider.supports(payload.direction)]
    if not applicable:
        return ChainOutcome(text=payload.text)

    text = payload.text
    verdicts: list[GuardrailVerdict] = []
    for chained in applicable:
        verdict = await _check(chained, replace(payload, text=text))
        verdicts.append(verdict)
        if verdict.decision is Decision.BLOCK:
            raise GuardrailBlocked(_refusal(verdict))
        if verdict.decision is Decision.REDACT and verdict.redacted_text is not None:
            text = verdict.redacted_text
    return ChainOutcome(text=text, verdicts=tuple(verdicts))


def _refusal(verdict: GuardrailVerdict) -> str:
    """The refusal message: provider, reason and categories — never the matched
    content, which is the thing the guardrail exists to keep out of sight."""
    return (
        f"blocked by {verdict.provider}"
        + (f": {verdict.reason}" if verdict.reason else "")
        + (f" ({', '.join(verdict.categories)})" if verdict.categories else "")
    )


async def _check(chained: ChainedProvider, payload: GuardrailPayload) -> GuardrailVerdict:
    """One provider's verdict, with its own failure resolved by its policy.

    The exception never escapes: an unreachable moderation endpoint must not
    surface to the caller as a 500 from someone else's guardrail. It becomes
    either ALLOW or BLOCK, and either way it is logged with the provider name
    and never the payload.
    """
    provider = chained.provider
    try:
        return await provider.check(payload)
    except Exception as exc:
        logger.warning(
            "guardrail provider %s failed (%s policy)",
            provider.name,
            chained.fail.value,
            exc_info=True,
        )
        if chained.fail is FailPolicy.CLOSED:
            return GuardrailVerdict(
                decision=Decision.BLOCK,
                provider=provider.name,
                reason=f"provider unavailable ({type(exc).__name__})",
            )
        return GuardrailVerdict(decision=Decision.ALLOW, provider=provider.name)


def redacted_payload(payload: GuardrailPayload, outcome: ChainOutcome) -> GuardrailPayload:
    """The payload as the next stage should see it."""
    return replace(payload, text=outcome.text)
