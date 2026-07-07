"""S4 — LLM as a judge.

A small, fast team model (the *judge*, picked by the admin) receives the user
text (truncated to a char budget) plus each candidate's name, description and
quality tier, and must answer via **constrained structured output**: a
`json_schema` response format whose `choice` property is an enum over the
candidate names — never free-text parsing. The gateway's own cross-provider
structured-output translation makes this work on any provider. Any failure —
timeout, refusal, malformed output, non-candidate choice — hits the §4 net:
fallback to `default_model`, the request never fails.

The judge prompt is a versioned constant, deliberately not admin-editable in v1.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from litestar_gateway.domain.routing import CandidateModel, RoutingContext, RoutingDecision

STRATEGY_ID = "judge"
DEFAULT_CHAR_BUDGET = 4000

JUDGE_PROMPT_V1 = (
    "You route user requests to the most appropriate model. "
    "Pick the CHEAPEST candidate that can still answer well: prefer lower "
    "tiers (SIMPLE < MEDIUM < COMPLEX < REASONING) unless the task clearly "
    "needs more. Answer with the required JSON only."
)

# async (judge_model_name, request_dict) -> OpenAI-shaped response dict
CompleteFn = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class JudgeStrategy:
    def __init__(
        self, config: dict[str, Any] | None = None, *, complete: CompleteFn | None = None
    ) -> None:
        config = config or {}
        judge_model = config.get("judge_model")
        if not (isinstance(judge_model, str) and judge_model):
            raise ValueError("judge strategy requires a 'judge_model' (team chat model name)")
        self._judge_model = judge_model
        self._char_budget = int(config.get("char_budget", DEFAULT_CHAR_BUDGET))
        self._complete = complete

    def _request(
        self, ctx: RoutingContext, candidates: tuple[CandidateModel, ...]
    ) -> dict[str, Any]:
        lines = [f"- {c.model_name} [{c.quality_tier}]: {c.description}" for c in candidates]
        user = (
            f"Candidates:\n{chr(10).join(lines)}\n\n"
            f"User request (may be truncated):\n{ctx.user_text[: self._char_budget]}"
        )
        return {
            "model": self._judge_model,
            "messages": [
                {"role": "system", "content": JUDGE_PROMPT_V1},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "routing_choice",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "choice": {
                                "type": "string",
                                "enum": [c.model_name for c in candidates],
                            }
                        },
                        "required": ["choice"],
                        "additionalProperties": False,
                    },
                },
            },
        }

    async def select(
        self, ctx: RoutingContext, candidates: tuple[CandidateModel, ...]
    ) -> RoutingDecision:
        complete = self._complete
        if complete is None:
            raise ValueError("judge strategy is missing its completion dependency")
        start = perf_counter()
        response = await complete(self._judge_model, self._request(ctx, candidates))
        content = response["choices"][0]["message"]["content"]
        choice = json.loads(content)["choice"]
        if choice not in {c.model_name for c in candidates}:
            raise ValueError(f"judge chose non-candidate {choice!r}")
        return RoutingDecision(
            model_name=choice,
            strategy=STRATEGY_ID,
            tier=None,
            score=None,
            signals=(f"judge {self._judge_model} chose {choice}",),
            decision_ms=(perf_counter() - start) * 1000,
        )
