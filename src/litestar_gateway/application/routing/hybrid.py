"""S5 — hybrid gray-zone: composition, not a new algorithm.

Runs S1 (rule-based complexity). If the weighted score lands within a
configurable margin of a tier boundary, the case is "uncertain" and the
escalation strategy (the judge or the webhook, per config) makes the final
call; otherwise S1's answer stands. `RoutingDecision.signals` records whether
escalation fired — this yields ~judge quality at a fraction of judge cost.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

from litestar_gateway.application.routing.complexity import (
    DEFAULT_TIER_BOUNDARIES,
    ComplexityStrategy,
)
from litestar_gateway.domain.routing import CandidateModel, RoutingContext, RoutingDecision

STRATEGY_ID = "hybrid"
DEFAULT_MARGIN = 0.08
_ESCALATION_STRATEGIES = ("judge", "webhook")


class HybridStrategy:
    """Built by the RouterService, which supplies the escalation strategy
    instance (with its deps bound) via `escalation`."""

    def __init__(
        self, config: dict[str, Any] | None = None, *, escalation: Any | None = None
    ) -> None:
        config = config or {}
        name = config.get("escalation_strategy")
        if name not in _ESCALATION_STRATEGIES:
            raise ValueError(
                f"hybrid strategy requires 'escalation_strategy' in {_ESCALATION_STRATEGIES}"
            )
        self.escalation_name: str = name
        # The escalation strategy's own config lives under config["escalation"].
        self.escalation_config: dict[str, Any] = config.get("escalation", {})
        margin = config.get("margin", DEFAULT_MARGIN)
        if not isinstance(margin, (int, float)) or margin <= 0:
            raise ValueError("hybrid 'margin' must be a positive number")
        self._margin = float(margin)
        self._rule_based = ComplexityStrategy(config)
        self._boundaries = {**DEFAULT_TIER_BOUNDARIES, **config.get("tier_boundaries", {})}
        self._escalation = escalation

    def _is_uncertain(self, score: float) -> bool:
        return any(abs(score - b) <= self._margin for b in self._boundaries.values())

    async def select(
        self, ctx: RoutingContext, candidates: tuple[CandidateModel, ...]
    ) -> RoutingDecision:
        start = perf_counter()
        decision = await self._rule_based.select(ctx, candidates)
        score = decision.score if decision.score is not None else 0.0
        if not self._is_uncertain(score):
            return RoutingDecision(
                model_name=decision.model_name,
                strategy=STRATEGY_ID,
                tier=decision.tier,
                score=decision.score,
                signals=(*decision.signals, "gray-zone: no"),
                decision_ms=(perf_counter() - start) * 1000,
            )
        if self._escalation is None:
            raise ValueError("hybrid strategy is missing its escalation dependency")
        escalated = await self._escalation.select(ctx, candidates)
        return RoutingDecision(
            model_name=escalated.model_name,
            strategy=STRATEGY_ID,
            tier=decision.tier,
            score=decision.score,
            signals=(
                *decision.signals,
                f"gray-zone: escalated to {self.escalation_name}",
                *escalated.signals,
            ),
            decision_ms=(perf_counter() - start) * 1000,
        )
