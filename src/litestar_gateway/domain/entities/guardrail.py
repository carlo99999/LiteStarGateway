"""A configured guardrail: one provider, in one chain, for one team.

A rule is *ordered* (`position`) because redactions compose in sequence, and
*scoped* — to one model (`model_id`) because a team that guards everything still
needs to say "except this model", or to one router (`router_id`) because the
alias a caller asks for is the stable thing to guard when the candidates behind
it change. The provider's own knobs live in `config`, validated per
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
    # Scope to the *router* the caller asked for, rather than to whichever
    # candidate the strategy picked. Attaching the policy to the candidates
    # instead leaves a hole that opens silently: add a candidate to the router
    # later and it is unguarded. At most one of `model_id`/`router_id` is set —
    # enforced on write, since a rule scoped to both would have no coherent
    # meaning.
    router_id: UUID | None = None
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
    rules: list[GuardrailRule],
    *,
    model_id: UUID,
    direction: Direction,
    router_id: UUID | None = None,
) -> list[GuardrailRule]:
    """The ordered rules that apply to one call on one side of it.

    Scoped rules **replace** the broader ones rather than adding to them.
    Merging would make a team-wide rule impossible to relax for a single
    model: an operator who guards the whole team and then needs one model
    exempted (an internal summarizer over already-classified text, say) has no
    way to express that if both sets always apply. Overriding gives them
    "team default, unless this says otherwise", which is how the rest of
    the model config already behaves.

    Three tiers, most specific first: **router, then model, then team-wide.**
    `router_id` is the router the caller named, or `None` for a direct call.

    The router outranking the resolved model is deliberate. The caller asked
    for the router; which candidate serves it is the gateway's choice. Were a
    candidate's own rule to win, attaching a rule to one candidate would
    quietly exempt it from the router's guard — the hole this scope exists to
    close. A direct call to that same model still gets the model's rule, so the
    per-model exemption keeps working where it was meant to.
    """
    applicable = [r for r in rules if r.enabled and r.direction is direction]
    by_router = [r for r in applicable if r.router_id == router_id] if router_id is not None else []
    by_model = [r for r in applicable if r.router_id is None and r.model_id == model_id]
    team_wide = [r for r in applicable if r.router_id is None and r.model_id is None]
    chosen = by_router or by_model or team_wide
    # Position first, then name: two rules at the same position must still order
    # deterministically, because a chain of redactors that composes differently
    # on each replica produces different text for the same prompt.
    return sorted(chosen, key=lambda r: (r.position, r.name))
