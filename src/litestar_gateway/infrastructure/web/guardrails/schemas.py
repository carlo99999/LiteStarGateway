"""DTOs for guardrail rules.

No response type carries the signing secret — only `has_secret`. That is not
politeness: the repository never hands the value to this layer at all, so there
is nothing here that could accidentally serialize it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from litestar.params import Parameter

from litestar_gateway.domain.entities import GuardrailRule


@dataclass(frozen=True)
class CreateGuardrailRuleRequest:
    """Add one provider to the team's chain."""

    name: Annotated[str, Parameter(description="Operator-facing name, unique in the team.")]
    kind: Annotated[str, Parameter(description="`webhook` or `judge`.")]
    direction: Annotated[
        str, Parameter(description="`request` (before the call) or `response` (after it).")
    ]
    config: Annotated[
        dict[str, Any],
        Parameter(
            description=(
                "Provider knobs. webhook: `url` (https, required), `timeout_ms`. "
                "judge: `judge_model` (required), `block_categories`, `char_budget`. "
                "Unknown keys are rejected rather than ignored."
            )
        ),
    ]
    fail_policy: Annotated[
        str,
        Parameter(
            description=(
                "What this provider's own failure means: `closed` refuses the "
                "request (a control that could not run has not passed), `open` "
                "lets it through (the guardrail is advisory)."
            )
        ),
    ] = "closed"
    position: Annotated[
        int, Parameter(description="Order within the chain; redactions compose in this order.")
    ] = 0
    model_id: Annotated[
        UUID | None,
        Parameter(
            description=(
                "Scope the rule to one model of this team. Omit for team-wide. "
                "Model-scoped rules REPLACE the team-wide ones for that model."
            )
        ),
    ] = None
    router_id: Annotated[
        UUID | None,
        Parameter(
            description=(
                "Scope the rule to one router of this team — the alias the caller "
                "asks for, rather than whichever candidate the strategy picks. "
                "OUTRANKS a model-scoped rule on the resolved model. Mutually "
                "exclusive with model_id."
            )
        ),
    ] = None
    enabled: Annotated[bool, Parameter(description="Whether the rule runs.")] = True
    signing_secret: Annotated[
        str | None,
        Parameter(
            description=(
                "HMAC secret this gateway signs webhook calls with. Required for "
                "`webhook`; never returned by any endpoint."
            )
        ),
    ] = None


@dataclass(frozen=True)
class UpdateGuardrailRuleRequest:
    """Partial update. Omitted fields are unchanged — including
    `signing_secret`, which cannot be read back and so cannot be resubmitted."""

    name: Annotated[str | None, Parameter(description="New name.")] = None
    direction: Annotated[str | None, Parameter(description="`request` or `response`.")] = None
    config: Annotated[
        dict[str, Any] | None,
        Parameter(description="Replaces the whole config; validated as a whole."),
    ] = None
    fail_policy: Annotated[str | None, Parameter(description="`open` or `closed`.")] = None
    position: Annotated[int | None, Parameter(description="Order within the chain.")] = None
    model_id: Annotated[UUID | None, Parameter(description="Scope to one model.")] = None
    router_id: Annotated[
        UUID | None,
        Parameter(description="Scope to one router; outranks a model-scoped rule."),
    ] = None
    enabled: Annotated[bool | None, Parameter(description="Enable or disable the rule.")] = None
    signing_secret: Annotated[
        str | None, Parameter(description="Rotate the HMAC secret. Omit to keep the current one.")
    ] = None


@dataclass(frozen=True)
class GuardrailRuleResponse:
    id: UUID
    team_id: UUID
    name: str
    kind: str
    direction: str
    position: int
    fail_policy: str
    enabled: bool
    has_secret: bool
    model_id: UUID | None = None
    router_id: UUID | None = None
    config: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_entity(cls, rule: GuardrailRule) -> GuardrailRuleResponse:
        return cls(
            id=rule.id,
            team_id=rule.team_id,
            model_id=rule.model_id,
            router_id=rule.router_id,
            name=rule.name,
            kind=rule.kind.value,
            direction=rule.direction.value,
            position=rule.position,
            fail_policy=rule.fail_policy.value,
            enabled=rule.enabled,
            has_secret=rule.has_secret,
            config=dict(rule.config),
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )
