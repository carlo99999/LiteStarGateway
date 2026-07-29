"""Guardrails: the policy layer that inspects a request before it is sent and a
response before it is billed.

Three verdicts, in increasing order of severity — `ALLOW`, `REDACT`, `BLOCK` —
and a provider is anything that can turn a payload into one of them. Providers
are independent by construction: the chain runner fans them out concurrently and
combines their verdicts, so adding one never changes what another decides.

What a verdict may carry back is deliberately narrow: categories and counts,
never the matched text. A guardrail exists because some content is sensitive;
echoing it into an audit row, a log line or an error body would move the problem
rather than solve it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class Direction(Enum):
    """Which side of the call is being inspected. A provider may support one or
    both; the chain runner only ever hands it a payload for a direction it
    declared."""

    REQUEST = "request"
    RESPONSE = "response"


class Decision(Enum):
    ALLOW = "allow"
    REDACT = "redact"
    BLOCK = "block"


class FailPolicy(Enum):
    """What a provider's own failure means — a timeout, a malformed response, an
    unreachable endpoint.

    `OPEN` lets the request through: the guardrail is advisory and availability
    wins. `CLOSED` refuses it: the guardrail is a control, and a control that
    cannot be evaluated has not passed. Per provider, because a PII redactor and
    a compliance blocker do not deserve the same answer.
    """

    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True)
class GuardrailPayload:
    """What a provider is asked to judge.

    `text` is the flattened, human-readable content — the user's prompt on the
    request side, the model's answer on the response side. `raw` is the full
    body for providers that need structure (tool calls, message roles); most
    only need the text.
    """

    direction: Direction
    text: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GuardrailVerdict:
    """One provider's answer.

    `categories` names what was found (`"pii.email"`, `"moderation.violence"`),
    `counts` how many of each. Neither carries the matched content, and
    `redacted_text` is the rewritten payload — the only place modified content
    appears, and it goes to the provider, never to a log.
    """

    decision: Decision
    provider: str
    categories: tuple[str, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)
    redacted_text: str | None = None
    # Why a CLOSED-policy provider refused, for the audit row. Never the payload.
    reason: str | None = None


@runtime_checkable
class GuardrailProvider(Protocol):
    """A single check. Implementations must be side-effect free with respect to
    the request: the chain runner may call several concurrently, discard a
    verdict when another provider blocks first, or skip the response side
    entirely if the request was blocked."""

    name: str

    def supports(self, direction: Direction) -> bool: ...

    async def check(self, payload: GuardrailPayload) -> GuardrailVerdict: ...
