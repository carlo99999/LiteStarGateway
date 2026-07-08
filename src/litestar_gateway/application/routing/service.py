"""RouterService — router CRUD (validated) + the routing decision itself.

Failure policy (§4): a strategy failure must never fail the user request —
any exception or timeout falls back to `default_model` (or, when the §3 filters
excluded it, the first capable candidate) and is recorded with
`fallback_used=True`. Zero capable candidates, by contrast, is a router
misconfiguration and does fail the request (`NoRoutableCandidate`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from litestar_gateway.application.routing.complexity import ComplexityStrategy
from litestar_gateway.application.routing.embeddings import EmbeddingsStrategy
from litestar_gateway.application.routing.hybrid import HybridStrategy
from litestar_gateway.application.routing.judge import JudgeStrategy
from litestar_gateway.application.routing.webhook import WebhookStrategy
from litestar_gateway.application.routing.weighted import WeightedStrategy
from litestar_gateway.domain.entities import ModelType
from litestar_gateway.domain.exceptions import (
    InvalidRouterConfig,
    NoRoutableCandidate,
    RouterNameExists,
    RouterNotFound,
)
from litestar_gateway.domain.ports import (
    CredentialRepository,
    LLMGateway,
    ModelRepository,
    RouterRepository,
    RoutingDecisionLog,
    RoutingDecisionLogFactory,
    RoutingRepositoryFactory,
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

STRATEGIES: dict[str, type] = {
    "complexity": ComplexityStrategy,
    "webhook": WebhookStrategy,
    "embeddings": EmbeddingsStrategy,
    "judge": JudgeStrategy,
    "hybrid": HybridStrategy,
    "weighted": WeightedStrategy,
}

# Hard time budget for a strategy call. The rule-based strategy is local and
# sub-millisecond; the budget exists so future network strategies (webhook,
# LLM judge) can never stall the request path.
DEFAULT_TIME_BUDGET_MS = 2000


def _now() -> datetime:
    return datetime.now(UTC)


# Strong refs to in-flight shadow tasks: a bare create_task() result may be
# garbage-collected mid-flight, silently cancelling the shadow run.
_SHADOW_TASKS: set[asyncio.Task] = set()

# How long shutdown waits for in-flight shadow tasks before cancelling them.
SHADOW_DRAIN_TIMEOUT_S = 5.0


async def drain_shadow_tasks(timeout: float | None = None) -> None:
    """Await in-flight fire-and-forget shadow tasks on shutdown (R7-M51).

    Each shadow run does its own `session_maker()` unit of work; if it is still
    in flight when the SQLAlchemy plugin disposes the engine, the write races
    teardown and the shadow decision/usage row is silently lost. The
    infrastructure lifespan calls this before engine disposal so those tasks
    settle (or are cancelled) instead of being abandoned.

    Bounded by `timeout` seconds (defaults to `SHADOW_DRAIN_TIMEOUT_S`); any
    task still running past the deadline is cancelled. Pure asyncio — no
    framework imports — so the `infrastructure` layer can call it without the
    `application` layer reaching back into `infrastructure`/`litestar`.
    """
    deadline = SHADOW_DRAIN_TIMEOUT_S if timeout is None else timeout
    tasks = list(_SHADOW_TASKS)
    if not tasks:
        return
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=deadline)
    except TimeoutError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class RouterService:
    def __init__(
        self,
        routers: RouterRepository,
        models: ModelRepository,
        decisions: RoutingDecisionLog,
        shadow_decisions: RoutingDecisionLogFactory | None = None,
        credentials: CredentialRepository | None = None,
        gateway: LLMGateway | None = None,
        shadow_repos: RoutingRepositoryFactory | None = None,
    ) -> None:
        self._routers = routers
        self._models = models
        self._decisions = decisions
        self._shadow_decisions = shadow_decisions
        self._credentials = credentials
        self._gateway = gateway
        self._shadow_repos = shadow_repos
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
        await self._validate_strategy_deps(router, router.strategy, router.strategy_config)
        if router.shadow_strategy is not None:
            await self._validate_strategy_deps(
                router, router.shadow_strategy, router.strategy_config.get("shadow", {})
            )

    async def _validate_strategy_deps(self, router: RouterConfig, name: str, config: dict) -> None:
        """Config checks needing repository lookups (pure shape checks already
        ran via strategy instantiation in _validate)."""
        if name == "embeddings":
            await self._validate_embeddings_config(router, config)
        if name == "judge":
            await self._validate_judge_config(router, config)
        if name == "weighted":
            self._validate_weighted_candidates(router)
        if name == "hybrid":
            shell = HybridStrategy(config)
            await self._validate_strategy_deps(
                router, shell.escalation_name, shell.escalation_config
            )

    def _validate_weighted_candidates(self, router: RouterConfig) -> None:
        for candidate in router.candidates:
            if not isinstance(candidate.weight, (int, float)) or candidate.weight <= 0:
                raise InvalidRouterConfig(
                    f"weighted strategy requires a positive 'weight' on every "
                    f"candidate; '{candidate.model_name}' has {candidate.weight!r}"
                )

    async def _validate_judge_config(self, router: RouterConfig, config: dict) -> None:
        judge_name = config.get("judge_model")
        model = await self._models.get_by_name(router.team_id, judge_name) if judge_name else None
        if model is None or not model.enabled or model.type is not ModelType.CHAT:
            raise InvalidRouterConfig(f"'{judge_name}' is not an enabled chat model of this team")

    async def _validate_embeddings_config(self, router: RouterConfig, config: dict) -> None:
        model_name = config.get("embedding_model")
        model = await self._models.get_by_name(router.team_id, model_name) if model_name else None
        if model is None or not model.enabled or model.type is not ModelType.EMBEDDINGS:
            raise InvalidRouterConfig(
                f"'{model_name}' is not an enabled embeddings model of this team"
            )
        candidate_names = {candidate.model_name for candidate in router.candidates}
        for route in config.get("routes", []):
            if route.get("target_model") not in candidate_names:
                raise InvalidRouterConfig(
                    f"route '{route.get('name')}' targets a non-candidate model"
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
        ctx = dataclasses.replace(ctx, default_model=router.default_model)
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

        await self._persist(router, decision, fallback_used, api_key_id, capable, ctx)
        if router.shadow_strategy is not None and self._shadow_decisions is not None:
            # §6: fire-and-forget — the shadow's would-be decision is persisted
            # alongside the real one, never blocking or failing the request.
            task = asyncio.create_task(self._run_shadow(router, ctx, capable, api_key_id))
            _SHADOW_TASKS.add(task)
            task.add_done_callback(_SHADOW_TASKS.discard)
        return decision

    async def _embed_texts(
        self,
        team_id: UUID,
        model_name: str,
        texts: list[str],
        models: ModelRepository,
        credentials: CredentialRepository | None,
    ) -> list[list[float]]:
        """Embed via the gateway's own port, using the team's embedding model."""
        if self._gateway is None or credentials is None:
            raise ValueError("embeddings strategy is not wired (no gateway/credentials)")
        model = await models.get_by_name(team_id, model_name)
        if model is None or not model.enabled or model.type is not ModelType.EMBEDDINGS:
            raise ValueError(f"'{model_name}' is not an enabled embeddings model of this team")
        values = await credentials.get_values(model.credential_id)
        if values is None:
            raise ValueError(f"credential missing for embedding model '{model_name}'")
        response = await self._gateway.aembeddings(
            {"model": model_name, "input": texts}, model, values
        )
        return [item["embedding"] for item in response["data"]]

    async def _judge_complete(
        self,
        team_id: UUID,
        model_name: str,
        request: dict[str, Any],
        models: ModelRepository,
        credentials: CredentialRepository | None,
    ) -> dict[str, Any]:
        """One non-streamed chat call to the judge, via the gateway's own port."""
        if self._gateway is None or credentials is None:
            raise ValueError("judge strategy is not wired (no gateway/credentials)")
        model = await models.get_by_name(team_id, model_name)
        if model is None or not model.enabled or model.type is not ModelType.CHAT:
            raise ValueError(f"'{model_name}' is not an enabled chat model of this team")
        values = await credentials.get_values(model.credential_id)
        if values is None:
            raise ValueError(f"credential missing for judge model '{model_name}'")
        return await self._gateway.achat_completion(request, model, values)

    def _build_strategy(
        self,
        router: RouterConfig,
        name: str,
        config: dict[str, Any],
        *,
        models: ModelRepository | None = None,
        credentials: CredentialRepository | None = None,
    ):
        models = self._models if models is None else models
        credentials = self._credentials if credentials is None else credentials
        if name == "embeddings":

            async def embed(model_name: str, texts: list[str]) -> list[list[float]]:
                return await self._embed_texts(
                    router.team_id, model_name, texts, models, credentials
                )

            return EmbeddingsStrategy(config, embed=embed)
        if name == "judge":

            async def complete(model_name: str, request: dict[str, Any]) -> dict[str, Any]:
                return await self._judge_complete(
                    router.team_id, model_name, request, models, credentials
                )

            return JudgeStrategy(config, complete=complete)
        if name == "hybrid":
            shell = HybridStrategy(config)  # validates margin/escalation name
            escalation = self._build_strategy(
                router,
                shell.escalation_name,
                shell.escalation_config,
                models=models,
                credentials=credentials,
            )
            return HybridStrategy(config, escalation=escalation)
        return STRATEGIES[name](config)

    async def _run_strategy(self, router, ctx, capable) -> tuple[RoutingDecision, bool]:
        start = perf_counter()
        budget_ms = router.strategy_config.get("time_budget_ms", DEFAULT_TIME_BUDGET_MS)
        try:
            strategy = self._build_strategy(router, router.strategy, router.strategy_config)
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
        # §4 fallback must respect the §3 hard filters: use default_model only
        # if it is capable; otherwise pick the first capable candidate (in
        # declared order — deterministic) and record why it was skipped.
        fallback_model = router.default_model
        signals: tuple[str, ...] = ("fallback",)
        if not any(fallback_model == c.model_name for c in capable):
            fallback_model = capable[0].model_name
            signals = ("fallback", "default_model skipped: lacks required capability")
            logger.warning(
                "router %s: default_model %r lacks a required capability; "
                "falling back to first capable candidate %r",
                router.name,
                router.default_model,
                fallback_model,
            )
        return (
            RoutingDecision(
                model_name=fallback_model,
                strategy=router.strategy,
                tier=None,
                score=None,
                signals=signals,
                decision_ms=(perf_counter() - start) * 1000,
            ),
            True,
        )

    async def _run_shadow(self, router, ctx, capable, api_key_id) -> None:
        """Run the shadow strategy and persist its verdict with is_shadow=True.
        Same time budget as an active strategy; failures logged and swallowed."""
        try:
            shadow_config = router.strategy_config.get("shadow", {})
            decision = await self._shadow_select(router, ctx, capable, shadow_config)
            if self._shadow_decisions is None:  # pragma: no cover - guarded by caller
                return
            user_text, system_prompt = self._distillation_text(decision, ctx)
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
                        user_text=user_text,
                        system_prompt=system_prompt,
                    )
                )
        except Exception:
            logger.warning(
                "router %s: shadow strategy %s failed (swallowed)",
                router.name,
                router.shadow_strategy,
                exc_info=True,
            )

    async def _shadow_select(self, router, ctx, capable, config) -> RoutingDecision:
        """Run the shadow strategy within its time budget. Its DB lookups race
        the request coroutine (still using the request-scoped session), so the
        strategy gets repositories on its own session via `_shadow_repos` —
        the same care `_shadow_decisions` takes for the decision log."""
        budget_ms = config.get("time_budget_ms", DEFAULT_TIME_BUDGET_MS)
        if self._shadow_repos is None:
            strategy = self._build_strategy(router, router.shadow_strategy, config)
            async with asyncio.timeout(budget_ms / 1000):
                return await strategy.select(ctx, capable)
        async with self._shadow_repos() as (models, credentials):
            strategy = self._build_strategy(
                router, router.shadow_strategy, config, models=models, credentials=credentials
            )
            async with asyncio.timeout(budget_ms / 1000):
                return await strategy.select(ctx, capable)

    @staticmethod
    def _unit_costs(
        chosen_name: str, candidates: tuple[CandidateModel, ...]
    ) -> tuple[float | None, float | None, float | None, float | None]:
        """(chosen_in, chosen_out, alt_in, alt_out) unit costs for savings (§7):
        `alt` is the most expensive capable candidate — what the request would
        have cost without routing. A candidate counts as priced when either
        cost is set; its missing side reads as 0.0 so partially-priced
        candidates still compete (and survive the savings query's NOT NULL
        filter). None when profiles carry no costs."""
        chosen = next((c for c in candidates if c.model_name == chosen_name), None)
        priced = [
            c
            for c in candidates
            if c.input_cost_per_token is not None or c.output_cost_per_token is not None
        ]
        alt = max(
            priced,
            key=lambda c: (c.input_cost_per_token or 0) + (c.output_cost_per_token or 0),
            default=None,
        )
        return (
            chosen.input_cost_per_token if chosen else None,
            chosen.output_cost_per_token if chosen else None,
            (alt.input_cost_per_token or 0.0) if alt else None,
            (alt.output_cost_per_token or 0.0) if alt else None,
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

    @staticmethod
    def _distillation_text(decision: RoutingDecision, ctx) -> tuple[str | None, str | None]:
        """§S6: keep the text only for judge decisions and hybrid escalations —
        the data a distilled classifier trains on. Everything else stays
        payload-free (privacy default)."""
        judged = decision.strategy == "judge" or any(
            "escalated" in signal for signal in decision.signals
        )
        if not judged:
            return None, None
        return ctx.user_text, ctx.system_prompt

    async def _persist(self, router, decision, fallback_used, api_key_id, capable, ctx) -> None:
        """Decision observability must never fail the request."""
        chosen_in, chosen_out, alt_in, alt_out = self._unit_costs(decision.model_name, capable)
        user_text, system_prompt = self._distillation_text(decision, ctx)
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
                    user_text=user_text,
                    system_prompt=system_prompt,
                )
            )
            self.last_decision_record_id = record_id
        except Exception:
            logger.warning(
                "router %s: failed to persist routing decision", router.name, exc_info=True
            )
