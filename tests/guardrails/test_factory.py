"""Turning stored rules into live providers.

The case that matters is a rule that cannot be instantiated. Skipping it
silently would disable a control while the console still shows it enabled, so
the rule's own fail policy decides — the same knob that already covers "the
provider timed out", because from the caller's side those are one situation.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from litestar_gateway.application.guardrails.factory import build_chain
from litestar_gateway.application.guardrails.judge import DEFAULT_CHAR_BUDGET
from litestar_gateway.application.guardrails.webhook import DEFAULT_TIMEOUT_MS
from litestar_gateway.domain.entities import ActiveGuardrailRule, GuardrailKind, GuardrailRule
from litestar_gateway.domain.exceptions import GuardrailBlocked
from litestar_gateway.domain.guardrails import Direction, FailPolicy

SIGNING_MATERIAL = "webhook-signing-material"  # pragma: allowlist secret


async def _complete(model: str, request: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
    return {}


def _active(
    kind: GuardrailKind,
    config: dict[str, Any],
    *,
    name: str = "rule",
    secret: str | None = SIGNING_MATERIAL,
    fail: FailPolicy = FailPolicy.CLOSED,
    direction: Direction = Direction.REQUEST,
) -> ActiveGuardrailRule:
    return ActiveGuardrailRule(
        rule=GuardrailRule(
            id=uuid4(),
            team_id=uuid4(),
            name=name,
            kind=kind,
            direction=direction,
            position=0,
            fail_policy=fail,
            config=config,
            has_secret=secret is not None,
        ),
        secret=secret,
    )


def test_each_rule_becomes_a_provider_named_after_it() -> None:
    # The name is what appears in a verdict and in the console, so it has to be
    # the operator's name, not the kind.
    chain = build_chain(
        [
            _active(GuardrailKind.WEBHOOK, {"url": "https://scan.example/x"}, name="pii-scan"),
            _active(GuardrailKind.JUDGE, {"judge_model": "moderator"}, name="moderation"),
        ],
        complete=_complete,
    )

    assert [c.provider.name for c in chain] == ["pii-scan", "moderation"]
    assert [c.fail for c in chain] == [FailPolicy.CLOSED, FailPolicy.CLOSED]


def test_a_provider_only_supports_its_rules_direction() -> None:
    chain = build_chain(
        [_active(GuardrailKind.JUDGE, {"judge_model": "m"}, direction=Direction.RESPONSE)],
        complete=_complete,
    )
    provider = chain[0].provider

    assert provider.supports(Direction.RESPONSE)
    assert not provider.supports(Direction.REQUEST)


def test_unset_knobs_keep_the_providers_own_defaults() -> None:
    # Not re-declared here: a default written in two places is a default that
    # will disagree with itself.
    chain = build_chain(
        [
            _active(GuardrailKind.WEBHOOK, {"url": "https://scan.example/x"}),
            _active(GuardrailKind.JUDGE, {"judge_model": "m"}, name="j"),
        ],
        complete=_complete,
    )

    assert chain[0].provider._timeout_seconds == DEFAULT_TIMEOUT_MS / 1000  # type: ignore[attr-defined]
    assert chain[1].provider._char_budget == DEFAULT_CHAR_BUDGET  # type: ignore[attr-defined]


def test_configured_knobs_are_passed_through() -> None:
    chain = build_chain(
        [
            _active(GuardrailKind.WEBHOOK, {"url": "https://scan.example/x", "timeout_ms": 750}),
            _active(
                GuardrailKind.JUDGE,
                {"judge_model": "m", "char_budget": 500, "block_categories": ["hate"]},
                name="j",
            ),
        ],
        complete=_complete,
    )

    assert chain[0].provider._timeout_seconds == 0.75  # type: ignore[attr-defined]
    assert chain[1].provider._char_budget == 500  # type: ignore[attr-defined]
    assert chain[1].provider._block_categories == frozenset({"hate"})  # type: ignore[attr-defined]


def test_an_unbuildable_closed_rule_refuses_the_request() -> None:
    # A webhook whose secret failed to decrypt cannot sign, so it cannot run.
    # `closed` means a control that did not run has not passed.
    with pytest.raises(GuardrailBlocked, match="could not be evaluated"):
        build_chain(
            [_active(GuardrailKind.WEBHOOK, {"url": "https://scan.example/x"}, secret=None)],
            complete=_complete,
        )


def test_an_unbuildable_open_rule_is_skipped_and_the_rest_still_runs() -> None:
    chain = build_chain(
        [
            _active(
                GuardrailKind.WEBHOOK,
                {"url": "https://scan.example/x"},
                name="broken",
                secret=None,
                fail=FailPolicy.OPEN,
            ),
            _active(GuardrailKind.JUDGE, {"judge_model": "m"}, name="works"),
        ],
        complete=_complete,
    )

    assert [c.provider.name for c in chain] == ["works"]


def test_a_judge_rule_without_a_completion_seam_follows_its_fail_policy() -> None:
    # Library use with no gateway wired: a judge cannot run at all.
    with pytest.raises(GuardrailBlocked):
        build_chain([_active(GuardrailKind.JUDGE, {"judge_model": "m"})], complete=None)

    assert (
        build_chain(
            [_active(GuardrailKind.JUDGE, {"judge_model": "m"}, fail=FailPolicy.OPEN)],
            complete=None,
        )
        == ()
    )


def test_no_rules_is_an_empty_chain() -> None:
    assert build_chain([], complete=_complete) == ()
