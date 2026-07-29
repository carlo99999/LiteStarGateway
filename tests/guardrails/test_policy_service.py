"""What the policy service enforces on top of the repository.

Which permission each operation demands is the part worth pinning: a guardrail
that the person configuring models can switch off is not a control, so
`guardrails:manage` is deliberately not part of `model_manager`'s grant, and
every entry point asks for it explicitly.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from litestar_gateway.application.guardrail_policy_service import GuardrailPolicyService
from litestar_gateway.domain.authorization import Permission
from litestar_gateway.domain.entities import (
    ActiveGuardrailRule,
    GuardrailKind,
    GuardrailRule,
    Model,
    ModelType,
    Principal,
    Provider,
)
from litestar_gateway.domain.exceptions import (
    GuardrailRuleNotFound,
    InvalidGuardrailRule,
    ModelNotFound,
    PermissionDenied,
)
from litestar_gateway.domain.guardrails import Direction, FailPolicy

TEAM = uuid4()
OTHER_TEAM = uuid4()
PRINCIPAL = Principal(user=None, api_key=None)
JUDGE_CONFIG = {"judge_model": "moderator"}
SIGNING_MATERIAL = "webhook-signing-material"  # pragma: allowlist secret


class FakeTeams:
    """Records what permission was demanded; refuses the ones it was told to."""

    def __init__(self, *, denied: set[Permission] | None = None) -> None:
        self.asked: list[tuple[UUID, Permission]] = []
        self._denied = denied or set()

    async def ensure_principal_team_permission(
        self, principal: Principal, team_id: UUID, permission: Permission
    ) -> None:
        self.asked.append((team_id, permission))
        if permission in self._denied:
            raise PermissionDenied(str(permission))


class FakeRules:
    def __init__(self) -> None:
        self.stored: dict[UUID, GuardrailRule] = {}
        self.secrets: dict[UUID, str | None] = {}

    async def list_for_team(self, team_id: UUID) -> list[GuardrailRule]:
        return [r for r in self.stored.values() if r.team_id == team_id]

    async def get(self, team_id: UUID, rule_id: UUID) -> GuardrailRule | None:
        rule = self.stored.get(rule_id)
        return rule if rule and rule.team_id == team_id else None

    async def add(self, rule: GuardrailRule, *, secret: str | None = None) -> GuardrailRule:
        self.stored[rule.id] = rule
        self.secrets[rule.id] = secret
        return rule

    async def update(self, rule: GuardrailRule, *, secret: str | None = None) -> GuardrailRule:
        self.stored[rule.id] = rule
        if secret is not None:
            self.secrets[rule.id] = secret
        return rule

    async def remove(self, team_id: UUID, rule_id: UUID) -> bool:
        rule = self.stored.get(rule_id)
        if rule is None or rule.team_id != team_id:
            return False
        del self.stored[rule_id]
        return True

    async def resolve(
        self, team_id: UUID, model_id: UUID, direction: Direction
    ) -> list[ActiveGuardrailRule]:  # pragma: no cover - not exercised here
        return []


class FakeModels:
    def __init__(self, *models: Model) -> None:
        self._models = {m.id: m for m in models}

    async def get(self, model_id: UUID) -> Model | None:
        return self._models.get(model_id)


def _model(team_id: UUID) -> Model:
    return Model(
        id=uuid4(),
        team_id=team_id,
        name="m",
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gpt-4o",
        params={},
        api_version=None,
        input_cost_per_token=None,
        output_cost_per_token=None,
        enabled=True,
        created_at=None,  # type: ignore[arg-type]
    )


def _service(
    *, teams: FakeTeams | None = None, models: FakeModels | None = None
) -> tuple[GuardrailPolicyService, FakeRules, FakeTeams]:
    rules, team_service = FakeRules(), teams or FakeTeams()
    service = GuardrailPolicyService(
        rules,  # type: ignore[arg-type]
        team_service,  # type: ignore[arg-type]
        models,  # type: ignore[arg-type]
    )
    return service, rules, team_service


async def _create(service: GuardrailPolicyService, name: str = "moderation", **kwargs):
    return await service.create_rule(
        PRINCIPAL,
        TEAM,
        name=name,
        kind=kwargs.pop("kind", GuardrailKind.JUDGE),
        direction=kwargs.pop("direction", Direction.REQUEST),
        fail_policy=kwargs.pop("fail_policy", FailPolicy.CLOSED),
        config=kwargs.pop("config", dict(JUDGE_CONFIG)),
        **kwargs,
    )


# ── Authorization ─────────────────────────────────────────────────────────────


async def test_reads_demand_read_and_writes_demand_manage() -> None:
    service, _, teams = _service()
    stored = await _create(service)
    await service.list_rules(PRINCIPAL, TEAM)
    await service.get_rule(PRINCIPAL, TEAM, stored.id)
    await service.update_rule(PRINCIPAL, TEAM, stored.id, position=3)
    await service.delete_rule(PRINCIPAL, TEAM, stored.id)

    assert [p for _, p in teams.asked] == [
        Permission.GUARDRAILS_MANAGE,  # create
        Permission.GUARDRAILS_READ,  # list
        Permission.GUARDRAILS_READ,  # get
        Permission.GUARDRAILS_MANAGE,  # update
        Permission.GUARDRAILS_MANAGE,  # delete
    ]
    # Every check was scoped to the team in the path, never to the rule's own
    # stored team — which is what a cross-team edit would exploit.
    assert {t for t, _ in teams.asked} == {TEAM}


async def test_a_caller_without_manage_cannot_change_the_policy() -> None:
    teams = FakeTeams(denied={Permission.GUARDRAILS_MANAGE})
    service, rules, _ = _service(teams=teams)

    with pytest.raises(PermissionDenied):
        await _create(service)
    assert rules.stored == {}


async def test_a_caller_without_read_cannot_list_the_policy() -> None:
    service, _, _ = _service(teams=FakeTeams(denied={Permission.GUARDRAILS_READ}))
    with pytest.raises(PermissionDenied):
        await service.list_rules(PRINCIPAL, TEAM)


# ── Invariants ────────────────────────────────────────────────────────────────


async def test_duplicate_name_is_a_domain_error_not_a_constraint_violation() -> None:
    service, _, _ = _service()
    await _create(service, "moderation")

    with pytest.raises(InvalidGuardrailRule, match="already exists"):
        await _create(service, "moderation")


async def test_renaming_onto_an_existing_name_is_refused() -> None:
    service, _, _ = _service()
    first = await _create(service, "first")
    await _create(service, "second")

    with pytest.raises(InvalidGuardrailRule, match="already exists"):
        await service.update_rule(PRINCIPAL, TEAM, first.id, name="second")
    # Keeping its own name is not a clash with itself.
    assert (await service.update_rule(PRINCIPAL, TEAM, first.id, name="first")).name == "first"


async def test_a_model_scoped_rule_must_name_a_model_of_this_team() -> None:
    # Otherwise the rule silently never fires, and the operator believes a model
    # is guarded when it is not.
    foreign = _model(OTHER_TEAM)
    service, _, _ = _service(models=FakeModels(foreign))

    with pytest.raises(ModelNotFound):
        await _create(service, model_id=foreign.id)
    with pytest.raises(ModelNotFound):
        await _create(service, "other", model_id=uuid4())


async def test_a_model_scoped_rule_accepts_a_model_of_this_team() -> None:
    own = _model(TEAM)
    service, _, _ = _service(models=FakeModels(own))

    stored = await _create(service, model_id=own.id)

    assert stored.model_id == own.id


async def test_update_validates_the_resulting_rule_not_the_diff() -> None:
    service, _, _ = _service()
    stored = await _create(service)

    # A partial edit that leaves `judge_model` untouched must still be judged
    # against the whole resulting config.
    with pytest.raises(InvalidGuardrailRule, match="char_budget"):
        await service.update_rule(
            PRINCIPAL, TEAM, stored.id, config={**JUDGE_CONFIG, "char_budget": 1}
        )


async def test_operations_on_another_teams_rule_are_not_found() -> None:
    service, rules, _ = _service()
    stored = await _create(service)
    rules.stored[stored.id] = replace(stored, team_id=OTHER_TEAM)

    with pytest.raises(GuardrailRuleNotFound):
        await service.get_rule(PRINCIPAL, TEAM, stored.id)
    with pytest.raises(GuardrailRuleNotFound):
        await service.update_rule(PRINCIPAL, TEAM, stored.id, position=1)
    with pytest.raises(GuardrailRuleNotFound):
        await service.delete_rule(PRINCIPAL, TEAM, stored.id)


async def test_webhook_rule_requires_a_secret_at_creation() -> None:
    service, _, _ = _service()
    config = {"url": "https://scanner.example/check"}

    with pytest.raises(InvalidGuardrailRule, match="signing secret"):
        await _create(service, "scanner", kind=GuardrailKind.WEBHOOK, config=config)

    stored = await _create(
        service,
        "scanner",
        kind=GuardrailKind.WEBHOOK,
        config=config,
        secret=SIGNING_MATERIAL,
    )
    assert stored.kind is GuardrailKind.WEBHOOK
