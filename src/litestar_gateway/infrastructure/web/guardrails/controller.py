"""Team-scoped guardrail rules (team-admin or platform-admin).

Authorization is not repeated here — `GuardrailPolicyService` demands
`guardrails:read` / `guardrails:manage` on every call, so a future entry point
inherits it instead of having to remember it.

Enum-shaped fields arrive as strings and are parsed here, at the boundary: a
bad `kind` is a 400 about the request, not a `ValueError` from the domain.
"""

from __future__ import annotations

from enum import Enum
from uuid import UUID

from litestar import Controller, Request, delete, get, patch, post
from litestar.di import NamedDependency, Provide
from litestar.params import FromPath

from litestar_gateway.application.guardrail_policy_service import GuardrailPolicyService
from litestar_gateway.domain.entities import GuardrailKind, Principal
from litestar_gateway.domain.exceptions import InvalidGuardrailRule
from litestar_gateway.domain.guardrails import Direction, FailPolicy
from litestar_gateway.domain.ports import AuditLog
from litestar_gateway.infrastructure.web.audit.recorder import record_audit
from litestar_gateway.infrastructure.web.guardrails.schemas import (
    CreateGuardrailRuleRequest,
    GuardrailRuleResponse,
    UpdateGuardrailRuleRequest,
)
from litestar_gateway.infrastructure.web.principal import provide_principal


def _require[EnumT: Enum](enum: type[EnumT], value: str, field: str) -> EnumT:
    try:
        return enum(value)
    except ValueError as exc:
        allowed = ", ".join(str(e.value) for e in enum)
        raise InvalidGuardrailRule(f"{field} must be one of: {allowed}") from exc


def _optional[EnumT: Enum](enum: type[EnumT], value: str | None, field: str) -> EnumT | None:
    return None if value is None else _require(enum, value, field)


class GuardrailController(Controller):
    path = "/teams"
    tags = ["guardrails"]
    dependencies = {"principal": Provide(provide_principal)}

    @get(
        "/{team_id:uuid}/guardrails",
        summary="List the team's guardrail rules",
        description="Ordered by position. Signing secrets are never returned.",
    )
    async def list_rules(
        self,
        team_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        guardrail_policy_service: NamedDependency[GuardrailPolicyService],
    ) -> list[GuardrailRuleResponse]:
        rules = await guardrail_policy_service.list_rules(principal, team_id)
        return [GuardrailRuleResponse.from_entity(rule) for rule in rules]

    @get(
        "/{team_id:uuid}/guardrails/{rule_id:uuid}",
        summary="Get one guardrail rule",
    )
    async def get_rule(
        self,
        team_id: FromPath[UUID],
        rule_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        guardrail_policy_service: NamedDependency[GuardrailPolicyService],
    ) -> GuardrailRuleResponse:
        rule = await guardrail_policy_service.get_rule(principal, team_id, rule_id)
        return GuardrailRuleResponse.from_entity(rule)

    @post(
        "/{team_id:uuid}/guardrails",
        summary="Add a guardrail rule",
        description=(
            "Adds one provider to the team's chain. A `webhook` rule requires an "
            "https url and a signing secret — the payload is the user's prompt, "
            "so unsigned cleartext egress is refused rather than defaulted."
        ),
    )
    async def create_rule(
        self,
        request: Request,
        team_id: FromPath[UUID],
        data: CreateGuardrailRuleRequest,
        principal: NamedDependency[Principal],
        guardrail_policy_service: NamedDependency[GuardrailPolicyService],
        audit_log: NamedDependency[AuditLog],
    ) -> GuardrailRuleResponse:
        rule = await guardrail_policy_service.create_rule(
            principal,
            team_id,
            name=data.name,
            kind=_require(GuardrailKind, data.kind, "kind"),
            direction=_require(Direction, data.direction, "direction"),
            fail_policy=_require(FailPolicy, data.fail_policy, "fail_policy"),
            config=data.config,
            position=data.position,
            model_id=data.model_id,
            router_id=data.router_id,
            enabled=data.enabled,
            secret=data.signing_secret,
        )
        await record_audit(
            audit_log,
            request,
            principal.user,
            "guardrail.create",
            target_type="guardrail_rule",
            target_id=rule.id,
            # The rule's identity and shape, never its config: a webhook url is
            # not a secret but it is an operational detail an audit reader does
            # not need, and the config is one edit away from carrying one.
            detail=f"{rule.kind.value} {rule.direction.value} '{rule.name}'",
        )
        return GuardrailRuleResponse.from_entity(rule)

    @patch(
        "/{team_id:uuid}/guardrails/{rule_id:uuid}",
        summary="Update a guardrail rule",
        description=(
            "Partial update. Omitting `signing_secret` keeps the stored one — it "
            "is never readable, so omission cannot mean 'clear'."
        ),
    )
    async def update_rule(
        self,
        request: Request,
        team_id: FromPath[UUID],
        rule_id: FromPath[UUID],
        data: UpdateGuardrailRuleRequest,
        principal: NamedDependency[Principal],
        guardrail_policy_service: NamedDependency[GuardrailPolicyService],
        audit_log: NamedDependency[AuditLog],
    ) -> GuardrailRuleResponse:
        rule = await guardrail_policy_service.update_rule(
            principal,
            team_id,
            rule_id,
            secret=data.signing_secret,
            name=data.name,
            direction=_optional(Direction, data.direction, "direction"),
            fail_policy=_optional(FailPolicy, data.fail_policy, "fail_policy"),
            config=data.config,
            position=data.position,
            model_id=data.model_id,
            router_id=data.router_id,
            enabled=data.enabled,
        )
        await record_audit(
            audit_log,
            request,
            principal.user,
            "guardrail.update",
            target_type="guardrail_rule",
            target_id=rule.id,
            detail=f"'{rule.name}' enabled={rule.enabled} fail={rule.fail_policy.value}",
        )
        return GuardrailRuleResponse.from_entity(rule)

    @delete(
        "/{team_id:uuid}/guardrails/{rule_id:uuid}",
        summary="Remove a guardrail rule",
        status_code=204,
    )
    async def delete_rule(
        self,
        request: Request,
        team_id: FromPath[UUID],
        rule_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        guardrail_policy_service: NamedDependency[GuardrailPolicyService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        await guardrail_policy_service.delete_rule(principal, team_id, rule_id)
        # Audited because removing a control is exactly the change an operator
        # will later need to explain.
        await record_audit(
            audit_log,
            request,
            principal.user,
            "guardrail.delete",
            target_type="guardrail_rule",
            target_id=rule_id,
        )
