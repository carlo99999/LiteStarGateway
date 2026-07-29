"""Port — persistence for configured guardrail rules."""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from litestar_gateway.domain.entities import ActiveGuardrailRule, GuardrailRule
from litestar_gateway.domain.guardrails import Direction


@runtime_checkable
class GuardrailRuleRepository(Protocol):
    """Per-team guardrail rules, ordered within a chain.

    Secrets are the repository's business alone: every method returns entities
    with `has_secret` set and the value withheld, except `resolve`, which the
    call path uses and which needs the secret to sign with. That asymmetry is
    the point — a management endpoint literally cannot leak what it never reads.
    """

    async def list_for_team(self, team_id: UUID) -> list[GuardrailRule]: ...

    async def get(self, team_id: UUID, rule_id: UUID) -> GuardrailRule | None: ...

    async def add(self, rule: GuardrailRule, *, secret: str | None = None) -> GuardrailRule: ...

    async def update(self, rule: GuardrailRule, *, secret: str | None = None) -> GuardrailRule:
        """Replace the rule. `secret=None` keeps the stored one — a secret that
        cannot be read back cannot be resubmitted, so omitting it must mean
        "unchanged" rather than "clear it"."""
        ...

    async def remove(self, team_id: UUID, rule_id: UUID) -> bool: ...

    async def resolve(
        self,
        team_id: UUID,
        model_id: UUID,
        direction: Direction,
        router_id: UUID | None = None,
    ) -> list[ActiveGuardrailRule]:
        """The ordered, enabled rules that apply to this call and direction,
        each with its secret. Per `domain.entities.guardrail.resolve_chain`,
        scoped rules override broader ones, most specific first: the router the
        caller named, then the resolved model, then team-wide. `router_id` is
        `None` for a direct model call."""
        ...
