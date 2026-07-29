"""Which rules apply, in what order, and which configurations are refused.

Two decisions carry the weight here: model-scoped rules **override** team-wide
ones rather than adding to them (so a per-model exemption is expressible at
all), and config validation is strict (so a typo cannot silently disable a
control).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from litestar_gateway.domain.entities import GuardrailKind, GuardrailRule, resolve_chain
from litestar_gateway.domain.exceptions import InvalidGuardrailRule
from litestar_gateway.domain.guardrail_config import validate_rule
from litestar_gateway.domain.guardrails import Direction, FailPolicy

TEAM = uuid4()
MODEL = uuid4()
OTHER_MODEL = uuid4()
SIGNING_MATERIAL = "webhook-signing-material"  # pragma: allowlist secret


def _rule(
    name: str,
    *,
    direction: Direction = Direction.REQUEST,
    position: int = 0,
    model_id=None,
    enabled: bool = True,
    kind: GuardrailKind = GuardrailKind.JUDGE,
    config: dict | None = None,
    has_secret: bool = False,
) -> GuardrailRule:
    return GuardrailRule(
        id=uuid4(),
        team_id=TEAM,
        model_id=model_id,
        name=name,
        kind=kind,
        direction=direction,
        position=position,
        fail_policy=FailPolicy.CLOSED,
        enabled=enabled,
        config=config if config is not None else {"judge_model": "moderator"},
        has_secret=has_secret,
    )


# ── Resolution ────────────────────────────────────────────────────────────────


def test_team_wide_rules_apply_to_every_model() -> None:
    rules = [_rule("a"), _rule("b")]
    assert [r.name for r in resolve_chain(rules, model_id=MODEL, direction=Direction.REQUEST)] == [
        "a",
        "b",
    ]


def test_model_scoped_rules_replace_the_team_wide_ones() -> None:
    # The decision: override, not merge. An operator who guards the whole team
    # and needs one model exempted (an internal summarizer over already
    # classified text) can only express that if the specific set wins outright.
    rules = [_rule("team-wide"), _rule("just-this-model", model_id=MODEL)]
    chain = resolve_chain(rules, model_id=MODEL, direction=Direction.REQUEST)

    assert [r.name for r in chain] == ["just-this-model"]
    # Every other model still gets the team-wide rule.
    other = resolve_chain(rules, model_id=OTHER_MODEL, direction=Direction.REQUEST)
    assert [r.name for r in other] == ["team-wide"]


def test_ordering_is_by_position_then_name() -> None:
    # Redactors compose in sequence, so the order must be total: two rules at
    # the same position would otherwise produce different text per replica.
    rules = [_rule("z", position=1), _rule("b", position=0), _rule("a", position=0)]
    chain = resolve_chain(rules, model_id=MODEL, direction=Direction.REQUEST)
    assert [r.name for r in chain] == ["a", "b", "z"]


def test_disabled_and_other_direction_rules_are_excluded() -> None:
    rules = [
        _rule("off", enabled=False),
        _rule("response-side", direction=Direction.RESPONSE),
        _rule("on"),
    ]
    chain = resolve_chain(rules, model_id=MODEL, direction=Direction.REQUEST)
    assert [r.name for r in chain] == ["on"]


def test_no_applicable_rules_is_an_empty_chain() -> None:
    # Which the call path treats exactly like having no guardrails at all.
    assert resolve_chain([], model_id=MODEL, direction=Direction.REQUEST) == []


# ── Validation: webhook ───────────────────────────────────────────────────────


def test_webhook_rule_needs_an_https_url() -> None:
    with pytest.raises(InvalidGuardrailRule, match="url"):
        validate_rule(
            _rule("w", kind=GuardrailKind.WEBHOOK, config={}),
            secret=SIGNING_MATERIAL,
        )
    with pytest.raises(InvalidGuardrailRule, match="https"):
        validate_rule(
            _rule("w", kind=GuardrailKind.WEBHOOK, config={"url": "http://x.example/check"}),
            secret=SIGNING_MATERIAL,
        )


def test_webhook_rule_without_a_signing_secret_is_refused() -> None:
    # The payload is the user's prompt. An unsigned egress is not something to
    # enable by omission — the receiver could not tell our call from anyone's.
    rule = _rule("w", kind=GuardrailKind.WEBHOOK, config={"url": "https://x.example/check"})
    with pytest.raises(InvalidGuardrailRule, match="signing secret"):
        validate_rule(rule)
    # Already stored counts: an edit that changes only the timeout must pass.
    validate_rule(
        _rule(
            "w",
            kind=GuardrailKind.WEBHOOK,
            config={"url": "https://x.example/check"},
            has_secret=True,
        )
    )


def test_unknown_config_key_is_an_error_not_a_default() -> None:
    # `timout_ms` would otherwise be dropped and the default applied, leaving
    # the operator with a policy nobody wrote.
    rule = _rule(
        "w",
        kind=GuardrailKind.WEBHOOK,
        config={"url": "https://x.example/check", "timout_ms": 500},
        has_secret=True,
    )
    with pytest.raises(InvalidGuardrailRule, match="timout_ms"):
        validate_rule(rule)


@pytest.mark.parametrize("bad", [0, 50, 10_001, True, "500", 1.5])
def test_timeout_must_be_an_integer_within_the_request_path_budget(bad: object) -> None:
    rule = _rule(
        "w",
        kind=GuardrailKind.WEBHOOK,
        config={"url": "https://x.example/check", "timeout_ms": bad},
        has_secret=True,
    )
    with pytest.raises(InvalidGuardrailRule, match="timeout_ms"):
        validate_rule(rule)


# ── Validation: judge ─────────────────────────────────────────────────────────


def test_judge_rule_needs_a_model() -> None:
    with pytest.raises(InvalidGuardrailRule, match="judge_model"):
        validate_rule(_rule("j", config={}))


def test_judge_block_categories_must_be_known() -> None:
    with pytest.raises(InvalidGuardrailRule, match="typo"):
        validate_rule(_rule("j", config={"judge_model": "m", "block_categories": ["typo"]}))
    validate_rule(_rule("j", config={"judge_model": "m", "block_categories": ["hate", "illicit"]}))


def test_judge_char_budget_is_bounded() -> None:
    with pytest.raises(InvalidGuardrailRule, match="char_budget"):
        validate_rule(_rule("j", config={"judge_model": "m", "char_budget": 20_001}))
    validate_rule(_rule("j", config={"judge_model": "m", "char_budget": 4000}))


# ── Validation: identity ──────────────────────────────────────────────────────


def test_name_and_position_are_validated() -> None:
    with pytest.raises(InvalidGuardrailRule, match="name"):
        validate_rule(_rule("   "))
    with pytest.raises(InvalidGuardrailRule, match="name"):
        validate_rule(_rule("x" * 101))
    with pytest.raises(InvalidGuardrailRule, match="position"):
        validate_rule(_rule("ok", position=-1))
