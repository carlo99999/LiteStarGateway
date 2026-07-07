"""Weighted multi-model routing: percentage-split traffic across candidates.

Each candidate declares a relative `weight` (any positive number — weights
need not sum to 100; the strategy normalizes over whichever candidates survive
the hard capability filters, so a filtered-out member's share is redistributed
proportionally among the rest). One weighted random draw per request selects
the member. Cost/usage attribution and the response's echoed `model` are
unaffected — both already follow the resolved member, not the router alias,
per the existing `_prepare` integration.
"""

from __future__ import annotations

import random
from bisect import bisect
from itertools import accumulate
from time import perf_counter
from typing import Any

from litestar_gateway.domain.routing import CandidateModel, RoutingContext, RoutingDecision

STRATEGY_ID = "weighted"


class WeightedStrategy:
    def __init__(self, config: dict[str, Any] | None = None, *, random_fn: Any = None) -> None:
        # No config of its own: weights live on the candidates themselves.
        self._random_fn = random_fn or random.random

    async def select(
        self, ctx: RoutingContext, candidates: tuple[CandidateModel, ...]
    ) -> RoutingDecision:
        start = perf_counter()
        weights: list[float] = []
        for candidate in candidates:
            weight = candidate.weight
            if not isinstance(weight, (int, float)) or weight <= 0:
                raise ValueError(
                    f"weighted strategy requires a positive 'weight' on every "
                    f"candidate; '{candidate.model_name}' has {weight!r}"
                )
            weights.append(weight)

        cumulative_weights = list(accumulate(weights))
        total = cumulative_weights[-1]
        draw = self._random_fn() * total
        index = min(bisect(cumulative_weights, draw), len(candidates) - 1)
        chosen, chosen_weight = candidates[index], weights[index]

        return RoutingDecision(
            model_name=chosen.model_name,
            strategy=STRATEGY_ID,
            tier=None,
            score=chosen_weight / total,
            signals=(f"weighted pick {chosen.model_name} ({chosen_weight}/{total})",),
            decision_ms=(perf_counter() - start) * 1000,
        )
