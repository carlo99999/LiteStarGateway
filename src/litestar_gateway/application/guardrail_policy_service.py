"""Application service for a team's guardrail chain.

Authorization is enforced here rather than in the controller, because a rule is
a safety control and the number of ways to reach it will only grow: a console
endpoint today, a management key tomorrow, a bulk import after that. Every path
goes through `TeamService.ensure_principal_team_permission` with
`guardrails:read` / `guardrails:manage`, which team admins and platform admins
hold and `model_manager` deliberately does not.

Validation lives in `domain.guardrail_config`; this service owns the ordering
and identity invariants (unique name per team, a model-scoped rule pointing at a
model the team can actually call).
"""

from __future__ import annotations

import dataclasses
from typing import Any
from uuid import UUID, uuid4

from litestar_gateway.application.team_service import TeamService
from litestar_gateway.domain.authorization import Permission
from litestar_gateway.domain.entities import GuardrailKind, GuardrailRule, Principal
from litestar_gateway.domain.exceptions import (
    GuardrailRuleNotFound,
    InvalidGuardrailRule,
    ModelNotFound,
    RouterNotFound,
)
from litestar_gateway.domain.guardrail_config import validate_rule
from litestar_gateway.domain.guardrails import Direction, FailPolicy
from litestar_gateway.domain.ports import (
    GuardrailRuleRepository,
    ModelRepository,
    RouterRepository,
)


class GuardrailPolicyService:
    def __init__(
        self,
        rules: GuardrailRuleRepository,
        teams: TeamService,
        models: ModelRepository | None = None,
        routers: RouterRepository | None = None,
    ) -> None:
        self._rules = rules
        self._teams = teams
        self._routers = routers
        # Optional only so library use can skip the model check; the web wiring
        # always passes one.
        self._models = models

    async def list_rules(self, principal: Principal, team_id: UUID) -> list[GuardrailRule]:
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.GUARDRAILS_READ
        )
        return await self._rules.list_for_team(team_id)

    async def get_rule(self, principal: Principal, team_id: UUID, rule_id: UUID) -> GuardrailRule:
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.GUARDRAILS_READ
        )
        return await self._require(team_id, rule_id)

    async def create_rule(
        self,
        principal: Principal,
        team_id: UUID,
        *,
        name: str,
        kind: GuardrailKind,
        direction: Direction,
        fail_policy: FailPolicy,
        config: dict[str, Any],
        position: int = 0,
        model_id: UUID | None = None,
        router_id: UUID | None = None,
        enabled: bool = True,
        secret: str | None = None,
    ) -> GuardrailRule:
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.GUARDRAILS_MANAGE
        )
        await self._ensure_model_visible(team_id, model_id)
        await self._ensure_router_visible(team_id, router_id)
        rule = GuardrailRule(
            id=uuid4(),
            team_id=team_id,
            model_id=model_id,
            router_id=router_id,
            name=name,
            kind=kind,
            direction=direction,
            position=position,
            fail_policy=fail_policy,
            enabled=enabled,
            config=config,
        )
        validate_rule(rule, secret=secret)
        await self._ensure_name_free(team_id, name)
        return await self._rules.add(rule, secret=secret)

    async def update_rule(
        self,
        principal: Principal,
        team_id: UUID,
        rule_id: UUID,
        *,
        secret: str | None = None,
        **changes: Any,
    ) -> GuardrailRule:
        """Apply the given non-None changes. `secret=None` keeps the stored
        signing secret — it is never readable, so omission cannot mean "clear"."""
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.GUARDRAILS_MANAGE
        )
        current = await self._require(team_id, rule_id)
        applied = {k: v for k, v in changes.items() if v is not None}
        updated = dataclasses.replace(current, **applied)
        await self._ensure_model_visible(team_id, updated.model_id)
        await self._ensure_router_visible(team_id, updated.router_id)
        # Validated as a whole: a partial edit that leaves the url untouched
        # must still be judged against the resulting rule, not the diff.
        validate_rule(updated, secret=secret)
        if updated.name != current.name:
            await self._ensure_name_free(team_id, updated.name)
        return await self._rules.update(updated, secret=secret)

    async def delete_rule(self, principal: Principal, team_id: UUID, rule_id: UUID) -> None:
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.GUARDRAILS_MANAGE
        )
        if not await self._rules.remove(team_id, rule_id):
            raise GuardrailRuleNotFound(str(rule_id))

    async def _require(self, team_id: UUID, rule_id: UUID) -> GuardrailRule:
        rule = await self._rules.get(team_id, rule_id)
        if rule is None:
            raise GuardrailRuleNotFound(str(rule_id))
        return rule

    async def _ensure_name_free(self, team_id: UUID, name: str) -> None:
        # Checked here for a clear 400 rather than relying on the unique
        # constraint's IntegrityError, which would surface as a 500.
        existing = await self._rules.list_for_team(team_id)
        if any(r.name == name for r in existing):
            raise InvalidGuardrailRule(f"a guardrail rule named '{name}' already exists")

    async def _ensure_router_visible(self, team_id: UUID, router_id: UUID | None) -> None:
        """Same reasoning as `_ensure_model_visible`: a rule scoped to another
        team's router would never fire, leaving the operator believing an
        alias is guarded when it is not."""
        if router_id is None or self._routers is None:
            return
        if await self._routers.get(team_id, router_id) is None:
            raise RouterNotFound(str(router_id))

    async def _ensure_model_visible(self, team_id: UUID, model_id: UUID | None) -> None:
        """A model-scoped rule must name a model of this team.

        Without this a team could scope a rule to another team's model id: the
        rule would never fire (resolution is team-scoped first), so the operator
        would believe a model is guarded when it is not — the failure mode a
        guardrail cannot afford.
        """
        if model_id is None or self._models is None:
            return
        model = await self._models.get(model_id)
        if model is None or model.team_id != team_id:
            raise ModelNotFound(str(model_id))
