"""A configured guardrail: one provider, in one chain, for one team.

A rule is *ordered* (`position`) because redactions compose in sequence, and
*scoped* (`model_id`) because a team that guards everything still needs to say
"except this model". The provider's own knobs live in `config`, validated per
kind — the alternative, a column per knob, would make adding a provider a
migration.

Secrets never appear here. `has_secret` says whether one is stored, so the
console can show "configured" without the value ever leaving the repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from litestar_gateway.domain.guardrails import Direction, FailPolicy


class GuardrailKind(Enum):
    """Which provider implementation a rule configures."""

    WEBHOOK = "webhook"
    JUDGE = "judge"


@dataclass(frozen=True)
class GuardrailRule:
    id: UUID
    team_id: UUID
    name: str
    kind: GuardrailKind
    direction: Direction
    position: int
    fail_policy: FailPolicy
    config: dict[str, Any] = field(default_factory=dict)
    # `None` applies the rule to every model the team can call. A rule bound to
    # one model takes precedence over the team-wide rules for that model —
    # `resolve_chain` overrides rather than merges, so a per-model exception is
    # expressible at all.
    model_id: UUID | None = None
    enabled: bool = True
    has_secret: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class ActiveGuardrailRule:
    """A rule together with the secret it needs to run.

    Kept separate from `GuardrailRule` so that every read path returns the
    entity without a secret by default, and only the call path — which must have
    it to sign a webhook — asks for this.
    """

    rule: GuardrailRule
    secret: str | None = None


def resolve_chain(
    rules: list[GuardrailRule], *, model_id: UUID, direction: Direction
) -> list[GuardrailRule]:
    """The ordered rules that apply to one model on one side of the call.

    Model-specific rules **replace** the team-wide ones rather than adding to
    them. Merging would make a team-wide rule impossible to relax for a single
    model: an operator who guards the whole team and then needs one model
    exempted (an internal summarizer over already-classified text, say) has no
    way to express that if both sets always apply. Overriding gives them
    "team default, unless this model says otherwise", which is how the rest of
    the model config already behaves.
    """
    applicable = [r for r in rules if r.enabled and r.direction is direction]
    specific = [r for r in applicable if r.model_id == model_id]
    chosen = specific or [r for r in applicable if r.model_id is None]
    # Position first, then name: two rules at the same position must still order
    # deterministically, because a chain of redactors that composes differently
    # on each replica produces different text for the same prompt.
    return sorted(chosen, key=lambda r: (r.position, r.name))
