"""RouterService — router CRUD (validated) + the routing decision itself.

Failure policy (§4): a strategy failure must never fail the user request —
any exception or timeout falls back to `default_model` and is recorded with
`fallback_used=True`. Zero capable candidates, by contrast, is a router
misconfiguration and does fail the request (`NoRoutableCandidate`).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from litestar_gateway.application.routing.complexity import ComplexityStrategy
from litestar_gateway.application.routing.webhook import WebhookStrategy
from litestar_gateway.domain.entities import ModelType
from litestar_gateway.domain.exceptions import (
    InvalidRouterConfig,
    NoRoutableCandidate,
    RouterNameExists,
    RouterNotFound,
)
from litestar_gateway.domain.ports import (
    ModelRepository,
    RouterRepository,
    RoutingDecisionLog,
    RoutingDecisionLogFactory,
)
from litestar_gateway.domain.routing import (
    CandidateModel,
    RouterConfig,
    RoutingDecision,
    RoutingDecisionRecord,
    build_routing_context,
    filter_candidates,
)

logger = logging.getLogger("litestar_gateway.routing")

STRATEGIES: dict[str, type] = {"complexity": ComplexityStrategy, "webhook": WebhookStrategy}

# Hard time budget for a strategy call. The rule-based strategy is local and
# sub-millisecond; the budget exists so future network strategies (webhook,
# LLM judge) can never stall the request path.
DEFAULT_TIME_BUDGET_MS = 2000


def _now() -> datetime:
    return datetime.now(UTC)


# Strong refs to in-flight shadow tasks: a bare create_task() result may be
# garbage-collected mid-flight, silently cancelling the shadow run.
_SHADOW_TASKS: set[asyncio.Task] = set()


class RouterService:
    def __init__(
        self,
        routers: RouterRepository,
        models: ModelRepository,
        decisions: RoutingDecisionLog,
        shadow_decisions: RoutingDecisionLogFactory | None = None,
    ) -> None:
        self._routers = routers
        self._models = models
        self._decisions = decisions
        self._shadow_decisions = shadow_decisions
        # The persisted record id of this request's routing decision (the
        # service is request-scoped), so settlement can attach actual usage.
        self.last_decision_record_id: UUID | None = None

    # ── CRUD (validated) ─────────────────────────────────────────────────────

    async def _validate(self, router: RouterConfig) -> None:
        if router.strategy not in STRATEGIES:
            raise InvalidRouterConfig(
                f"Unknown strategy '{router.strategy}'; available: {sorted(STRATEGIES)}"
            )
        try:
            STRATEGIES[router.strategy](router.strategy_config)
        except Exception as exc:
            raise InvalidRouterConfig(
                f"Invalid config for strategy '{router.strategy}': {exc}"
            ) from exc
        if router.shadow_strategy is not None:
            if router.shadow_strategy not in STRATEGIES:
                raise InvalidRouterConfig(
                    f"Unknown shadow strategy '{router.shadow_strategy}'; "
                    f"available: {sorted(STRATEGIES)}"
                )
            # The shadow strategy's config lives under strategy_config["shadow"].
            try:
                STRATEGIES[router.shadow_strategy](router.strategy_config.get("shadow", {}))
            except Exception as exc:
                raise InvalidRouterConfig(
                    f"Invalid config for shadow strategy '{router.shadow_strategy}': {exc}"
                ) from exc
        if not router.candidates:
            raise InvalidRouterConfig("A router needs at least one candidate")
        names = [candidate.model_name for candidate in router.candidates]
        if len(set(names)) != len(names):
            raise InvalidRouterConfig("Duplicate candidate model names")
        if router.default_model not in names:
            raise InvalidRouterConfig("default_model must be one of the candidates")
        # A router is a virtual model: its name must not shadow a real one.
        if await self._models.get_by_name(router.team_id, router.name) is not None:
            raise RouterNameExists(router.name)
        for name in names:
            model = await self._models.get_by_name(router.team_id, name)
            if model is None or not model.enabled or model.type is not ModelType.CHAT:
                raise InvalidRouterConfig(
                    f"Candidate '{name}' is not an enabled chat model of this team"
                )

    async def create(self, router: RouterConfig) -> RouterConfig:
        await self._validate(router)
        return await self._routers.add(router)

    async def update(self, router: RouterConfig) -> RouterConfig:
        await self._validate(router)
        return await self._routers.update(router)

    async def get(self, team_id: UUID, router_id: UUID) -> RouterConfig:
        router = await self._routers.get(team_id, router_id)
        if router is None:
            raise RouterNotFound(str(router_id))
        return router

    async def list_by_team(self, team_id: UUID) -> list[RouterConfig]:
        return await self._routers.list_by_team(team_id)

    async def delete(self, team_id: UUID, router_id: UUID) -> None:
        if not await self._routers.delete(team_id, router_id):
            raise RouterNotFound(str(router_id))

    async def get_enabled_by_name(self, team_id: UUID, name: str) -> RouterConfig | None:
        router = await self._routers.get_by_name(team_id, name)
        return router if router is not None and router.enabled else None

    # ── The decision ─────────────────────────────────────────────────────────

    async def route(
        self,
        router: RouterConfig,
        request: dict[str, Any],
        *,
        api_key_id: UUID | None = None,
    ) -> RoutingDecision:
        """Pick the candidate for this request and persist the decision.

        Raises only `NoRoutableCandidate` (a config problem); every strategy
        failure falls back to `default_model` per §4."""
        ctx = build_routing_context(request, team_id=router.team_id, api_key_id=api_key_id)
        capable = filter_candidates(ctx, router.candidates)
        if not capable:
            raise NoRoutableCandidate(
                f"Router '{router.name}': no candidate supports this request "
                "(vision/tools/json_schema/context-window filters left none)"
            )

        fallback_used = False
        if len(capable) == 1:  # §3: a single survivor skips the strategy
            decision = RoutingDecision(
                model_name=capable[0].model_name,
                strategy="capability-filter",
                tier=None,
                score=None,
                signals=("single capable candidate",),
                decision_ms=0.0,
            )
        else:
            decision, fallback_used = await self._run_strategy(router, ctx, capable)

        await self._persist(router, decision, fallback_used, api_key_id, capable)
        if router.shadow_strategy is not None and self._shadow_decisions is not None:
            # §6: fire-and-forget — the shadow's would-be decision is persisted
            # alongside the real one, never blocking or failing the request.
            task = asyncio.create_task(self._run_shadow(router, ctx, capable, api_key_id))
            _SHADOW_TASKS.add(task)
            task.add_done_callback(_SHADOW_TASKS.discard)
        return decision

    async def _run_strategy(self, router, ctx, capable) -> tuple[RoutingDecision, bool]:
        start = perf_counter()
        budget_ms = router.strategy_config.get("time_budget_ms", DEFAULT_TIME_BUDGET_MS)
        try:
            strategy = STRATEGIES[router.strategy](router.strategy_config)
            async with asyncio.timeout(budget_ms / 1000):
                decision = await strategy.select(ctx, capable)
            if any(decision.model_name == c.model_name for c in capable):
                return decision, False
            logger.warning(
                "router %s: strategy %s chose non-candidate %r; falling back",
                router.name,
                router.strategy,
                decision.model_name,
            )
        except Exception:
            logger.warning(
                "router %s: strategy %s failed; falling back to %s",
                router.name,
                router.strategy,
                router.default_model,
                exc_info=True,
            )
        return (
            RoutingDecision(
                model_name=router.default_model,
                strategy=router.strategy,
                tier=None,
                score=None,
                signals=("fallback",),
                decision_ms=(perf_counter() - start) * 1000,
            ),
            True,
        )

    async def _run_shadow(self, router, ctx, capable, api_key_id) -> None:
        """Run the shadow strategy and persist its verdict with is_shadow=True.
        Same time budget as an active strategy; failures logged and swallowed."""
        try:
            shadow_config = router.strategy_config.get("shadow", {})
            budget_ms = shadow_config.get("time_budget_ms", DEFAULT_TIME_BUDGET_MS)
            strategy = STRATEGIES[router.shadow_strategy](shadow_config)
            async with asyncio.timeout(budget_ms / 1000):
                decision = await strategy.select(ctx, capable)
            if self._shadow_decisions is None:  # pragma: no cover - guarded by caller
                return
            async with self._shadow_decisions() as log:
                await log.record(
                    RoutingDecisionRecord(
                        id=uuid4(),
                        team_id=router.team_id,
                        router_name=router.name,
                        strategy=decision.strategy,
                        chosen_model=decision.model_name,
                        tier=decision.tier,
                        score=decision.score,
                        signals=decision.signals,
                        decision_ms=decision.decision_ms,
                        is_shadow=True,
                        fallback_used=False,
                        api_key_id=api_key_id,
                        created_at=_now(),
                    )
                )
        except Exception:
            logger.warning(
                "router %s: shadow strategy %s failed (swallowed)",
                router.name,
                router.shadow_strategy,
                exc_info=True,
            )

    @staticmethod
    def _unit_costs(
        chosen_name: str, candidates: tuple[CandidateModel, ...]
    ) -> tuple[float | None, float | None, float | None, float | None]:
        """(chosen_in, chosen_out, alt_in, alt_out) unit costs for savings (§7):
        `alt` is the most expensive capable candidate — what the request would
        have cost without routing. None when profiles carry no costs."""
        chosen = next((c for c in candidates if c.model_name == chosen_name), None)
        priced = [c for c in candidates if c.input_cost_per_token is not None]
        alt = max(
            priced,
            key=lambda c: (c.input_cost_per_token or 0) + (c.output_cost_per_token or 0),
            default=None,
        )
        return (
            chosen.input_cost_per_token if chosen else None,
            chosen.output_cost_per_token if chosen else None,
            alt.input_cost_per_token if alt else None,
            alt.output_cost_per_token if alt else None,
        )

    async def record_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Attach actual usage to this request's decision. Never fails the request."""
        if self.last_decision_record_id is None:
            return
        try:
            await self._decisions.update_usage(
                self.last_decision_record_id, prompt_tokens, completion_tokens
            )
        except Exception:
            logger.warning("failed to attach usage to routing decision", exc_info=True)

    async def list_decisions(self, team_id: UUID, router_id: UUID, **filters):
        router = await self.get(team_id, router_id)
        return await self._decisions.list_decisions(team_id, router.name, **filters)

    async def stats(self, team_id: UUID, router_id: UUID) -> dict[str, Any]:
        router = await self.get(team_id, router_id)
        rows = await self._decisions.distribution(team_id, router.name)
        by_model: dict[str, int] = {}
        by_tier: dict[str, int] = {}
        shadow_by_model: dict[str, int] = {}
        for model_name, tier, is_shadow, count in rows:
            if is_shadow:
                shadow_by_model[model_name] = shadow_by_model.get(model_name, 0) + count
                continue
            by_model[model_name] = by_model.get(model_name, 0) + count
            if tier:
                by_tier[tier] = by_tier.get(tier, 0) + count
        return {
            "router": router.name,
            "total": sum(by_model.values()),
            "by_model": by_model,
            "by_tier": by_tier,
            "shadow_by_model": shadow_by_model,
        }

    async def savings(self, team_id: UUID, router_id: UUID) -> dict[str, Any]:
        router = await self.get(team_id, router_id)
        total, counted, without_usage = await self._decisions.savings(team_id, router.name)
        return {
            "router": router.name,
            "estimated_savings": total,
            "decisions_counted": counted,
            "decisions_without_usage": without_usage,
        }

    async def _persist(self, router, decision, fallback_used, api_key_id, capable) -> None:
        """Decision observability must never fail the request."""
        chosen_in, chosen_out, alt_in, alt_out = self._unit_costs(decision.model_name, capable)
        record_id = uuid4()
        try:
            await self._decisions.record(
                RoutingDecisionRecord(
                    id=record_id,
                    team_id=router.team_id,
                    router_name=router.name,
                    strategy=decision.strategy,
                    chosen_model=decision.model_name,
                    tier=decision.tier,
                    score=decision.score,
                    signals=decision.signals,
                    decision_ms=decision.decision_ms,
                    is_shadow=False,
                    fallback_used=fallback_used,
                    api_key_id=api_key_id,
                    created_at=_now(),
                    chosen_input_cost=chosen_in,
                    chosen_output_cost=chosen_out,
                    alt_input_cost=alt_in,
                    alt_output_cost=alt_out,
                )
            )
            self.last_decision_record_id = record_id
        except Exception:
            logger.warning(
                "router %s: failed to persist routing decision", router.name, exc_info=True
            )
