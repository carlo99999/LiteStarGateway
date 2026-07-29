"""Validation of a guardrail rule's `config`, per provider kind.

Strict on purpose: an unknown key is an error, not something to ignore. A
guardrail is a control, and the failure mode of a lenient parser here is the
worst one available — an operator types `timout_ms`, the value is dropped, the
default applies, and nothing anywhere says the policy is not the one they wrote.
The same reasoning makes every bound explicit rather than clamped silently.
"""

from __future__ import annotations

from typing import Any

from litestar_gateway.domain.entities.guardrail import GuardrailKind, GuardrailRule
from litestar_gateway.domain.exceptions import InvalidGuardrailRule

MAX_NAME_LENGTH = 100
# A guardrail runs inside the request path, so its timeout is the caller's
# added latency. 10 s is already past what any interactive client tolerates;
# past that, the rule is misconfigured rather than patient.
MAX_TIMEOUT_MS = 10_000
MIN_TIMEOUT_MS = 100
MIN_CHAR_BUDGET = 100
MAX_CHAR_BUDGET = 20_000

_WEBHOOK_KEYS = {"url", "timeout_ms"}
_JUDGE_KEYS = {"judge_model", "block_categories", "char_budget"}

JUDGE_CATEGORIES = (
    "harassment",
    "hate",
    "self_harm",
    "sexual",
    "violence",
    "illicit",
    "prompt_injection",
)


def validate_rule(rule: GuardrailRule, *, secret: str | None = None) -> None:
    """Reject a rule that could not run, or could run differently than written.

    `secret` is the value being stored by this write, if any: a webhook rule
    needs one to sign with, so a create without it is refused here rather than
    at the first guarded request.
    """
    if not rule.name.strip():
        raise InvalidGuardrailRule("name must not be empty")
    if len(rule.name) > MAX_NAME_LENGTH:
        raise InvalidGuardrailRule(f"name must be at most {MAX_NAME_LENGTH} characters")
    if rule.position < 0:
        raise InvalidGuardrailRule("position must be zero or positive")
    if rule.kind is GuardrailKind.WEBHOOK:
        _validate_webhook(rule.config, secret=secret, has_secret=rule.has_secret)
    else:
        _validate_judge(rule.config)


def _reject_unknown(config: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(config) - allowed)
    if unknown:
        raise InvalidGuardrailRule(
            f"unknown config key(s): {', '.join(unknown)}; allowed: {', '.join(sorted(allowed))}"
        )


def _validate_webhook(config: dict[str, Any], *, secret: str | None, has_secret: bool) -> None:
    _reject_unknown(config, _WEBHOOK_KEYS)
    url = config.get("url")
    if not isinstance(url, str) or not url:
        raise InvalidGuardrailRule("webhook guardrail requires a url")
    if not url.startswith("https://"):
        # The payload is the user's prompt. Cleartext is not a configuration
        # choice we let an operator make by accident, and there is a verdict
        # coming back that we are about to trust.
        raise InvalidGuardrailRule("webhook guardrail url must be https")
    if secret is None and not has_secret:
        # Without a secret the receiver cannot tell our call from anyone else's,
        # and an unsigned prompt egress is not something to enable by omission.
        raise InvalidGuardrailRule("webhook guardrail requires a signing secret")
    _validate_bounded_int(config, "timeout_ms", MIN_TIMEOUT_MS, MAX_TIMEOUT_MS)


def _validate_judge(config: dict[str, Any]) -> None:
    _reject_unknown(config, _JUDGE_KEYS)
    model = config.get("judge_model")
    if not isinstance(model, str) or not model:
        raise InvalidGuardrailRule("judge guardrail requires a judge_model")
    categories = config.get("block_categories")
    if categories is not None:
        if not isinstance(categories, list) or not all(isinstance(c, str) for c in categories):
            raise InvalidGuardrailRule("block_categories must be a list of strings")
        unknown = sorted(set(categories) - set(JUDGE_CATEGORIES))
        if unknown:
            raise InvalidGuardrailRule(
                f"unknown block_categories: {', '.join(unknown)}; "
                f"known: {', '.join(JUDGE_CATEGORIES)}"
            )
    _validate_bounded_int(config, "char_budget", MIN_CHAR_BUDGET, MAX_CHAR_BUDGET)


def _validate_bounded_int(config: dict[str, Any], key: str, low: int, high: int) -> None:
    value = config.get(key)
    if value is None:
        return  # unset ⇒ the provider's default
    # `bool` is an `int` in Python, and `True` as a timeout is a typo, not a 1ms
    # budget.
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidGuardrailRule(f"{key} must be an integer, got {value!r}")
    if not low <= value <= high:
        raise InvalidGuardrailRule(f"{key} must be between {low} and {high}, got {value}")
