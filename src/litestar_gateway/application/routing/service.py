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
import re
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from litestar_gateway.application.callable_aliases import CallableAliasResolver
from litestar_gateway.application.routing.complexity import ComplexityStrategy
from litestar_gateway.application.routing.embeddings import EmbeddingsStrategy
from litestar_gateway.application.routing.hybrid import HybridStrategy
from litestar_gateway.application.routing.judge import JudgeStrategy
from litestar_gateway.application.routing.webhook import WebhookStrategy
from litestar_gateway.application.routing.weighted import WeightedStrategy
from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.callable_alias import CallableKind
from litestar_gateway.domain.entities import Model, ModelType
from litestar_gateway.domain.exceptions import (
    InvalidRouterConfig,
    NoRoutableCandidate,
    RouterNameExists,
    RouterNotFound,
)
from litestar_gateway.domain.ports import (
    CallableModelResolver,
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
    RouterGrant,
    RoutingDecision,
    RoutingDecisionRecord,
    build_routing_context,
    filter_candidates,
)

logger = logging.getLogger("litestar_gateway.routing")


@dataclasses.dataclass(frozen=True)
class CallableRouter:
    """A router a team can call, with its effective alias and provenance.
    `origin` is one of `own`, `extended`, `global`."""

    alias: str
    router: RouterConfig
    origin: str
    source_team_id: UUID | None


def _slug(text: str) -> str:
    """A label safe to embed in a router alias (no spaces/punctuation)."""
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower() or "team"


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
_EXTERNAL_PREVIEW_STRATEGIES = frozenset({"embeddings", "judge", "webhook", "hybrid"})


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
        meter: UsageMeter | None = None,
        callable_resolver: CallableAliasResolver | None = None,
    ) -> None:
        self._routers = routers
        self._models = models
        self._decisions = decisions
        self._shadow_decisions = shadow_decisions
        self._credentials = credentials
        self._gateway = gateway
        self._shadow_repos = shadow_repos
        # Judge/embeddings strategies call the provider for real; the active path
        # meters those calls (budget-gated + billed). Shadow runs on a detached
        # task with its own session and cannot share this request-scoped meter.
        self._meter = meter
        self._callable_resolver = callable_resolver
        # The persisted record id of this request's routing decision (the
        # service is request-scoped), so settlement can attach actual usage.
        self.last_decision_record_id: UUID | None = None

    # ── CRUD (validated) ─────────────────────────────────────────────────────

    async def _resolve_model(
        self,
        team_id: UUID | None,
        name: str | None,
        models: ModelRepository | None = None,
        resolver: CallableModelResolver | None = None,
    ) -> Model | None:
        if not name:
            return None
        lookup = self._models if models is None else models
        if resolver is not None:
            return await resolver.resolve_model(team_id, name)
        if self._callable_resolver is not None and lookup is self._models:
            return await self._callable_resolver.resolve_model(team_id, name)
        return await lookup.get_by_name(team_id, name)

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
        if (
            self._callable_resolver is None
            and await self._models.get_by_name(router.team_id, router.name) is not None
        ):
            raise RouterNameExists(router.name)
        for name in names:
            model = await self._resolve_model(router.team_id, name)
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
        model = await self._resolve_model(router.team_id, judge_name)
        if model is None or not model.enabled or model.type is not ModelType.CHAT:
            raise InvalidRouterConfig(f"'{judge_name}' is not an enabled chat model of this team")

    async def _validate_embeddings_config(self, router: RouterConfig, config: dict) -> None:
        model_name = config.get("embedding_model")
        model = await self._resolve_model(router.team_id, model_name)
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
        if self._callable_resolver is not None and await self._callable_resolver.explicit_taken(
            router.team_id, router.name
        ):
            raise RouterNameExists(router.name)
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
        if self._callable_resolver is not None:
            resolved = await self._callable_resolver.resolve(team_id, name)
            if resolved is None or resolved.kind is not CallableKind.ROUTER:
                return None
            router = resolved.resource
            assert isinstance(router, RouterConfig)
            return router if router.enabled else None
        router = await self._routers.get_by_name(team_id, name)
        return router if router is not None and router.enabled else None

    # ── Platform (global) routers + extension ────────────────────────────────

    async def get_any(self, router_id: UUID) -> RouterConfig:
        router = await self._routers.get_any(router_id)
        if router is None:
            raise RouterNotFound(str(router_id))
        return router

    async def list_global(self) -> list[RouterConfig]:
        return await self._routers.list_global()

    async def update_global(self, router: RouterConfig) -> RouterConfig:
        """Update a global router (team_id is None; scoped by `update`)."""
        await self._validate(router)
        return await self._routers.update(router)

    async def delete_global(self, router_id: UUID) -> None:
        if not await self._routers.delete_global(router_id):
            raise RouterNotFound(str(router_id))

    async def make_global(self, router_id: UUID) -> RouterConfig:
        """Promote a team-owned router to a global (platform) one."""
        router = await self.get_any(router_id)
        if router.team_id is None:
            return router
        promoted = await self._routers.promote_to_global(router_id)
        if promoted is None:  # pragma: no cover - just fetched
            raise RouterNotFound(str(router_id))
        return promoted

    async def list_callable(self, team_id: UUID) -> list[CallableRouter]:
        """Every router a team can call, by effective alias: own → extended →
        global (own > extended > global on a clash; global gets `-global`)."""
        if self._callable_resolver is not None:
            resolved = await self._callable_resolver.list_callable(team_id)
            return [
                CallableRouter(
                    item.effective_alias,
                    item.resource,
                    item.binding.origin.value,
                    item.binding.source_team_id,
                )
                for item in resolved
                if item.kind is CallableKind.ROUTER and isinstance(item.resource, RouterConfig)
            ]

        by_alias: dict[str, CallableRouter] = {}
        for router in await self._routers.list_by_team(team_id):
            by_alias[router.name] = CallableRouter(router.name, router, "own", team_id)
        for grant in await self._routers.list_grants_for_team(team_id):
            source = await self._routers.get_any(grant.router_id)
            if source is not None and grant.alias not in by_alias:
                by_alias[grant.alias] = CallableRouter(
                    grant.alias, source, "extended", source.team_id
                )
        for router in await self._routers.all_global():
            alias = router.name if router.name not in by_alias else f"{router.name}-global"
            if alias not in by_alias:
                by_alias[alias] = CallableRouter(alias, router, "global", router.origin_team_id)
        return sorted(by_alias.values(), key=lambda c: c.alias)

    async def extend(
        self, router_id: UUID, source_label: str, team_ids: list[UUID]
    ) -> list[RouterGrant]:
        router = await self.get_any(router_id)
        label = _slug(source_label)
        grants: list[RouterGrant] = []
        existing = {g.team_id for g in await self._routers.list_grants_for_router(router_id)}
        for team_id in team_ids:
            if team_id == router.team_id or team_id in existing:
                continue
            alias = await self._disambiguate(team_id, router.name, label)
            grants.append(
                RouterGrant(
                    id=uuid4(),
                    router_id=router_id,
                    team_id=team_id,
                    alias=alias,
                    created_at=_now(),
                )
            )
        return await self._routers.add_grants(grants)

    async def _disambiguate(self, team_id: UUID, base: str, label: str) -> str:
        async def taken(alias: str) -> bool:
            if self._callable_resolver is not None:
                return await self._callable_resolver.slot_reserved(team_id, alias)
            return await self._routers.name_taken_in_team(team_id, alias)

        if not await taken(base):
            return base
        candidate = f"{base}-{label}"
        suffix = 2
        while await taken(candidate):
            candidate = f"{base}-{label}-{suffix}"
            suffix += 1
        return candidate

    async def list_grants(self, router_id: UUID) -> list[RouterGrant]:
        return await self._routers.list_grants_for_router(router_id)

    async def unextend(self, grant_id: UUID) -> None:
        await self._routers.remove_grant(grant_id)

    # ── The decision ─────────────────────────────────────────────────────────

    async def select_preview(
        self, router: RouterConfig, request: dict[str, Any], *, acting_team_id: UUID
    ) -> RoutingDecision:
        """Pick a candidate WITHOUT persisting a decision or running shadow — for
        the playground, which shows which candidate a router would choose without
        recording it. Strategies with provider or webhook side effects fall back
        to `default_model` here, since a preview has no billable caller identity."""
        ctx = build_routing_context(request, team_id=acting_team_id, api_key_id=None)
        ctx = dataclasses.replace(ctx, default_model=router.default_model)
        capable = filter_candidates(ctx, router.candidates)
        if not capable:
            raise NoRoutableCandidate(f"Router '{router.name}': no candidate supports this request")
        if len(capable) == 1:
            return RoutingDecision(
                model_name=capable[0].model_name,
                strategy="capability-filter",
                tier=None,
                score=None,
                signals=("single capable candidate",),
                decision_ms=0.0,
            )
        # Preview must never create a hidden provider/webhook side effect. The
        # governed completion immediately following this preview accounts for
        # the selected model call; external routing strategies are reserved for
        # the normal route(), where a request-scoped meter is available.
        if router.strategy in _EXTERNAL_PREVIEW_STRATEGIES:
            selected = next(
                (
                    candidate
                    for candidate in capable
                    if candidate.model_name == router.default_model
                ),
                capable[0],
            )
            return RoutingDecision(
                model_name=selected.model_name,
                strategy=router.strategy,
                tier=None,
                score=None,
                signals=("preview: external strategy skipped",),
                decision_ms=0.0,
            )
        decision, _ = await self._run_strategy(router, ctx, capable)
        return decision

    async def route(
        self,
        router: RouterConfig,
        request: dict[str, Any],
        *,
        acting_team_id: UUID,
        api_key_id: UUID | None = None,
    ) -> RoutingDecision:
        """Pick the candidate for this request and persist the decision.

        `acting_team_id` is the CALLING team — a global/extended router routes,
        meters (judge/embeddings strategies), and attributes its decisions in the
        caller's context, not the owner's. For a team-owned router it equals
        `router.team_id`. Raises only `NoRoutableCandidate` (a config problem);
        every strategy failure falls back to `default_model` per §4."""
        ctx = build_routing_context(request, team_id=acting_team_id, api_key_id=api_key_id)
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

    async def _metered_call(
        self,
        meter: UsageMeter | None,
        api_key_id: UUID | None,
        team_id: UUID,
        model,
        operation: str,
        request: dict[str, Any],
        call: Any,
    ) -> dict[str, Any]:
        """Run one internal strategy provider call through the meter — budget-gated
        at admission, billed + traced at settlement — mirroring
        `CompletionService._dispatch`. Without a meter/api_key_id (library use, or
        the shadow path, which runs on a detached task and cannot share the
        request-scoped meter) the call passes through unmetered."""
        if meter is None or api_key_id is None:
            return await call()
        reservation = await meter.admit(team_id, model, request)
        start = perf_counter()
        try:
            try:
                response = await call()
            except Exception as exc:
                meter.trace_error(
                    team_id, api_key_id, model, operation, (perf_counter() - start) * 1000, exc
                )
                raise
            await meter.settle_ok(
                team_id,
                api_key_id,
                model,
                operation,
                response,
                (perf_counter() - start) * 1000,
                request,
            )
            return response
        finally:
            meter.release(team_id, reservation)

    async def _embed_texts(
        self,
        team_id: UUID | None,
        model_name: str,
        texts: list[str],
        models: ModelRepository,
        credentials: CredentialRepository | None,
        resolver: CallableModelResolver | None = None,
        meter: UsageMeter | None = None,
        api_key_id: UUID | None = None,
    ) -> list[list[float]]:
        """Embed via the gateway's own port, using the team's embedding model."""
        # Metering needs a concrete team; the route path always passes the
        # acting team. None would be a wiring bug, not a global-router case.
        assert team_id is not None, "embeddings strategy requires an acting team"
        if self._gateway is None or credentials is None:
            raise ValueError("embeddings strategy is not wired (no gateway/credentials)")
        model = await self._resolve_model(team_id, model_name, models, resolver)
        if model is None or not model.enabled or model.type is not ModelType.EMBEDDINGS:
            raise ValueError(f"'{model_name}' is not an enabled embeddings model of this team")
        values = await credentials.get_values(model.credential_id)
        if values is None:
            raise ValueError(f"credential missing for embedding model '{model_name}'")
        request = {"model": model_name, "input": texts}
        response = await self._metered_call(
            meter,
            api_key_id,
            team_id,
            model,
            "routing.embeddings",
            request,
            lambda: self._gateway.aembeddings(request, model, values),
        )
        return [item["embedding"] for item in response["data"]]

    async def _judge_complete(
        self,
        team_id: UUID | None,
        model_name: str,
        request: dict[str, Any],
        models: ModelRepository,
        credentials: CredentialRepository | None,
        resolver: CallableModelResolver | None = None,
        meter: UsageMeter | None = None,
        api_key_id: UUID | None = None,
    ) -> dict[str, Any]:
        """One non-streamed chat call to the judge, via the gateway's own port."""
        assert team_id is not None, "judge strategy requires an acting team"
        if self._gateway is None or credentials is None:
            raise ValueError("judge strategy is not wired (no gateway/credentials)")
        model = await self._resolve_model(team_id, model_name, models, resolver)
        if model is None or not model.enabled or model.type is not ModelType.CHAT:
            raise ValueError(f"'{model_name}' is not an enabled chat model of this team")
        values = await credentials.get_values(model.credential_id)
        if values is None:
            raise ValueError(f"credential missing for judge model '{model_name}'")
        return await self._metered_call(
            meter,
            api_key_id,
            team_id,
            model,
            "routing.judge",
            request,
            lambda: self._gateway.achat_completion(request, model, values),
        )

    def _build_strategy(
        self,
        router: RouterConfig,
        name: str,
        config: dict[str, Any],
        *,
        models: ModelRepository | None = None,
        credentials: CredentialRepository | None = None,
        resolver: CallableModelResolver | None = None,
        meter: UsageMeter | None = None,
        api_key_id: UUID | None = None,
        acting_team_id: UUID | None = None,
    ):
        models = self._models if models is None else models
        credentials = self._credentials if credentials is None else credentials
        # The judge/embeddings strategies make real, billable provider calls;
        # resolve their model + bill them in the CALLING team's context (falls
        # back to the router's owner for team-owned routers / validation).
        team_id = acting_team_id if acting_team_id is not None else router.team_id
        if name == "embeddings":

            async def embed(model_name: str, texts: list[str]) -> list[list[float]]:
                return await self._embed_texts(
                    team_id,
                    model_name,
                    texts,
                    models,
                    credentials,
                    resolver,
                    meter,
                    api_key_id,
                )

            return EmbeddingsStrategy(config, embed=embed)
        if name == "judge":

            async def complete(model_name: str, request: dict[str, Any]) -> dict[str, Any]:
                return await self._judge_complete(
                    team_id,
                    model_name,
                    request,
                    models,
                    credentials,
                    resolver,
                    meter,
                    api_key_id,
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
                resolver=resolver,
                meter=meter,
                api_key_id=api_key_id,
                acting_team_id=acting_team_id,
            )
            return HybridStrategy(config, escalation=escalation)
        return STRATEGIES[name](config)

    async def _run_strategy(self, router, ctx, capable) -> tuple[RoutingDecision, bool]:
        start = perf_counter()
        budget_ms = router.strategy_config.get("time_budget_ms", DEFAULT_TIME_BUDGET_MS)
        try:
            strategy = self._build_strategy(
                router,
                router.strategy,
                router.strategy_config,
                meter=self._meter,
                api_key_id=ctx.api_key_id,
                acting_team_id=ctx.team_id,
            )
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
                        team_id=ctx.team_id,
                        router_id=router.id,
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
            strategy = self._build_strategy(
                router, router.shadow_strategy, config, acting_team_id=ctx.team_id
            )
            async with asyncio.timeout(budget_ms / 1000):
                return await strategy.select(ctx, capable)
        async with self._shadow_repos() as (models, credentials, resolver):
            strategy = self._build_strategy(
                router,
                router.shadow_strategy,
                config,
                models=models,
                credentials=credentials,
                resolver=resolver,
                acting_team_id=ctx.team_id,
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
        return await self._decisions.list_decisions(team_id, router.id, **filters)

    async def stats(self, team_id: UUID, router_id: UUID) -> dict[str, Any]:
        router = await self.get(team_id, router_id)
        rows = await self._decisions.distribution(team_id, router.id)
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
        total, counted, without_usage = await self._decisions.savings(team_id, router.id)
        return {
            "router": router.name,
            "estimated_savings": total,
            "decisions_counted": counted,
            "decisions_without_usage": without_usage,
        }

    async def platform_savings(self) -> dict[str, Any]:
        """Savings across every team and router — the platform-admin dashboard
        figure. Authorization is the caller's job (admin-only endpoint)."""
        total, counted, without_usage = await self._decisions.platform_savings()
        return {
            "estimated_savings": total,
            "decisions_counted": counted,
            "decisions_without_usage": without_usage,
        }

    async def team_savings(self, team_id: UUID) -> dict[str, Any]:
        """Savings for one team across all of its routers. Authorization is the
        caller's job (team USAGE_READ, checked at the endpoint)."""
        total, counted, without_usage = await self._decisions.team_savings(team_id)
        return {
            "team_id": str(team_id),
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
                    team_id=ctx.team_id,
                    router_id=router.id,
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
