"""Building a live chain from stored rules.

The interesting case is a rule that cannot be instantiated — a webhook whose
secret failed to decrypt, a config that predates a validation rule. Skipping it
would silently disable a control, so the rule's own fail policy decides, exactly
as it does for a provider that times out: CLOSED refuses the request, OPEN logs
and continues without it. That the same knob covers both is deliberate — from
the caller's perspective "the guardrail did not run" is one situation, not two.
"""

from __future__ import annotations

import logging
from typing import Any

from litestar_gateway.application.guardrails.judge import CompleteFn, JudgeGuardrail
from litestar_gateway.application.guardrails.service import ChainedProvider
from litestar_gateway.application.guardrails.webhook import WebhookGuardrail
from litestar_gateway.domain.entities import ActiveGuardrailRule, GuardrailKind, GuardrailRule
from litestar_gateway.domain.exceptions import GuardrailBlocked
from litestar_gateway.domain.guardrails import FailPolicy, GuardrailProvider

logger = logging.getLogger("litestar_gateway.guardrails")


def build_chain(
    active: list[ActiveGuardrailRule],
    *,
    complete: CompleteFn | None = None,
    client_factory: object = None,
) -> tuple[ChainedProvider, ...]:
    """Instantiate each rule's provider, in the order given."""
    chain: list[ChainedProvider] = []
    for entry in active:
        try:
            provider = _provider(entry, complete=complete, client_factory=client_factory)
        except Exception as exc:
            if entry.rule.fail_policy is FailPolicy.CLOSED:
                raise GuardrailBlocked(
                    f"guardrail '{entry.rule.name}' could not be evaluated: {exc}"
                ) from exc
            logger.warning(
                "skipping unbuildable guardrail rule",
                extra={"rule": entry.rule.name, "kind": entry.rule.kind.value},
                exc_info=True,
            )
            continue
        chain.append(ChainedProvider(provider=provider, fail=entry.rule.fail_policy))
    return tuple(chain)


def _provider(
    entry: ActiveGuardrailRule, *, complete: CompleteFn | None, client_factory: object
) -> GuardrailProvider:
    rule: GuardrailRule = entry.rule
    config = rule.config
    if rule.kind is GuardrailKind.WEBHOOK:
        if entry.secret is None:
            raise ValueError("webhook guardrail has no signing secret")
        return WebhookGuardrail(
            str(config["url"]),
            secret=entry.secret,
            name=rule.name,
            directions=(rule.direction,),
            client_factory=client_factory,
            **_optional(config, timeout_ms="timeout_ms"),
        )
    if complete is None:
        raise ValueError("judge guardrail is not wired (no completion seam)")
    categories = config.get("block_categories")
    knobs: dict[str, Any] = _optional(config, char_budget="char_budget")
    if categories:
        knobs["block_categories"] = tuple(categories)
    return JudgeGuardrail(
        str(config["judge_model"]),
        complete=complete,
        name=rule.name,
        directions=(rule.direction,),
        **knobs,
    )


def _optional(config: dict[str, Any], **mapping: str) -> dict[str, Any]:
    """Only pass through the knobs the operator actually set, so each provider's
    own default stays the single place a default is written down."""
    return {kwarg: config[key] for kwarg, key in mapping.items() if config.get(key) is not None}
