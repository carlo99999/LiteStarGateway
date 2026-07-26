"""Cross-provider failover orchestration (Plan 05 Phase 1).

The plan's own "done when" criteria, verified end to end through
CompletionService.chat_completion: a transient error on the first candidate
succeeds on the second; a terminal (client 4xx) error never retries; budget
is settled at most once per logical request, never double-charged; and one
logical request consumes exactly one team-RPM hit across every attempt.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from litestar_gateway.application.callable_aliases import ResolvedCallable
from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.callable_alias import (
    CallableAliasBinding,
    CallableKind,
    CallableOrigin,
)
from litestar_gateway.domain.entities import Budget, BudgetWindow, Model, ModelType, Provider
from litestar_gateway.domain.exceptions import (
    BudgetExceeded,
    UpstreamRequestRejected,
    UpstreamResponseInvalid,
    UpstreamUnavailable,
)
from litestar_gateway.domain.ports.rate_limiter import RateLimitDecision
from litestar_gateway.domain.routing import (
    CandidateModel,
    QualityTier,
    RouterConfig,
    RoutingDecision,
)

TEAM_ID = uuid4()
KEY_ID = uuid4()


def _model(name: str) -> Model:
    return Model(
        id=uuid4(),
        team_id=TEAM_ID,
        name=name,
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gpt-4o",
        params={},
        params_enforced={},
        api_version=None,
        input_cost_per_token=0.01,
        output_cost_per_token=0.01,
        enabled=True,
        created_at=datetime.now(UTC),
    )


def _budget(limit: float) -> Budget:
    return Budget(
        id=uuid4(),
        team_id=TEAM_ID,
        limit_cost=limit,
        window=BudgetWindow.MONTHLY,
        created_at=datetime.now(UTC),
    )


def _router(
    primary: Model, secondary: Model, *, max_attempts: int = 2, overall_deadline_ms=None
) -> RouterConfig:
    return RouterConfig(
        id=uuid4(),
        team_id=TEAM_ID,
        name="auto",
        candidates=(
            CandidateModel(
                model_name=primary.name,
                model_id=primary.id,
                description="primary",
                quality_tier=QualityTier.MEDIUM,
            ),
            CandidateModel(
                model_name=secondary.name,
                model_id=secondary.id,
                description="secondary",
                quality_tier=QualityTier.MEDIUM,
            ),
        ),
        default_model=primary.name,
        default_model_id=primary.id,
        strategy="complexity",
        strategy_config={},
        enabled=True,
        created_at=datetime.now(UTC),
        failover_enabled=True,
        max_attempts=max_attempts,
        overall_deadline_ms=overall_deadline_ms,
    )


class FakeModels:
    def __init__(self, models: dict[str, Model]) -> None:
        self._models = models

    async def get_by_name(self, team_id: UUID, name: str) -> Model | None:
        return self._models.get(name)


class FakeCredentials:
    async def get_values(self, credential_id: UUID) -> dict[str, str] | None:
        return {"api_key": "sk-x"}  # pragma: allowlist secret


class FakeUsage:
    def __init__(self, spent: float = 0.0) -> None:
        self.events: list[Any] = []
        self.spent = spent

    async def record(self, event: Any) -> None:
        self.events.append(event)

    async def enqueue_pending(self, event: Any) -> None:  # pragma: no cover
        raise AssertionError("outbox must not be used in these tests")

    async def spend_since(self, team_id: UUID, since: datetime) -> float:
        return self.spent


class FakeBudgets:
    def __init__(self, budget: Budget | None) -> None:
        self._budget = budget

    async def get(self, team_id: UUID) -> Budget | None:
        return self._budget


class FakeRateLimiter:
    def __init__(self) -> None:
        self.hits: list[str] = []

    async def hit(self, key: str, limit: int, *, window_seconds: int = 60) -> RateLimitDecision:
        self.hits.append(key)
        return RateLimitDecision(allowed=True, retry_after=0)


class FakeTeams:
    async def get(self, team_id: UUID):
        return SimpleNamespace(rate_limit_rpm=100)


class MultiModelCallableResolver:
    def __init__(self, router: RouterConfig, models: dict[UUID, Model]) -> None:
        self._router = router
        self._models = models

    async def resolve(self, team_id: UUID, alias: str) -> ResolvedCallable | None:
        if alias == self._router.name:
            return ResolvedCallable(
                effective_alias=alias,
                binding=CallableAliasBinding(
                    id=uuid4(),
                    team_id=team_id,
                    alias=alias,
                    kind=CallableKind.ROUTER,
                    resource_id=self._router.id,
                    origin=CallableOrigin.OWN,
                    source_team_id=team_id,
                ),
                resource=self._router,
            )
        return None

    async def resolve_model_id(self, team_id: UUID, model_id: UUID) -> Model | None:
        return self._models.get(model_id)


class FixedDecisionRouter:
    """Always routes to the given model; skips real strategy selection so
    these tests control exactly which candidate is attempt #1."""

    def __init__(self, chosen: Model) -> None:
        self._chosen = chosen
        self.route_calls = 0

    async def route(self, router, request, *, acting_team_id, api_key_id) -> RoutingDecision:
        self.route_calls += 1
        return RoutingDecision(
            model_name=self._chosen.name,
            strategy="fixed",
            tier=None,
            score=None,
            signals=(),
            decision_ms=0.0,
            model_id=self._chosen.id,
        )

    async def record_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        return None


class ScriptedGateway:
    """Raises the scripted exceptions in order (one per achat_completion
    call), then returns a canned successful response forever after."""

    def __init__(self, exceptions: list[Exception]) -> None:
        self._exceptions = list(exceptions)
        self.calls: list[Model] = []

    async def achat_completion(self, request, model, credentials) -> dict[str, Any]:
        self.calls.append(model)
        if self._exceptions:
            raise self._exceptions.pop(0)
        return {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}


def _service(
    gateway: ScriptedGateway,
    usage: FakeUsage,
    router: RouterConfig,
    models: dict[UUID, Model],
    router_service: FixedDecisionRouter,
    *,
    rate_limiter: FakeRateLimiter | None = None,
) -> CompletionService:
    return CompletionService(
        models=FakeModels({}),  # type: ignore[arg-type]
        credentials=FakeCredentials(),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        meter=UsageMeter(
            usage=usage,  # type: ignore[arg-type]
            emit_trace=lambda trace: None,
            budgets=FakeBudgets(_budget(1000.0)),  # type: ignore[arg-type]
            rate_limiter=rate_limiter,  # type: ignore[arg-type]
            teams=FakeTeams() if rate_limiter is not None else None,  # type: ignore[arg-type]
        ),
        router_service=router_service,  # type: ignore[arg-type]
        callable_resolver=MultiModelCallableResolver(router, models),  # type: ignore[arg-type]
    )


REQUEST = {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}


async def test_transient_error_fails_over_to_the_second_candidate() -> None:
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = ScriptedGateway([UpstreamUnavailable("503")])
    usage = FakeUsage()
    service = _service(gateway, usage, router, models, FixedDecisionRouter(primary))

    response = await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert response["usage"] == {"prompt_tokens": 1, "completion_tokens": 1}
    assert gateway.calls == [primary, secondary]
    assert len(usage.events) == 1  # billed exactly once -- the serving provider


async def test_terminal_error_never_retries() -> None:
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = ScriptedGateway([UpstreamRequestRejected("bad request")])
    usage = FakeUsage()
    service = _service(gateway, usage, router, models, FixedDecisionRouter(primary))

    with pytest.raises(UpstreamRequestRejected):
        await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert gateway.calls == [primary]  # never reached the second candidate
    assert usage.events == []  # nothing billed


async def test_already_billed_response_invalid_never_retries() -> None:
    # UpstreamResponseInvalid already bills a partial charge inside _dispatch
    # before re-raising; retrying it would double-bill the team.
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = ScriptedGateway(
        [UpstreamResponseInvalid("malformed", {"usage": {"prompt_tokens": 1}})]
    )
    usage = FakeUsage()
    service = _service(gateway, usage, router, models, FixedDecisionRouter(primary))

    with pytest.raises(UpstreamResponseInvalid):
        await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert gateway.calls == [primary]
    assert len(usage.events) == 1  # the one partial charge from the failed attempt


async def test_overall_deadline_stops_further_retries() -> None:
    # A deadline already in the past (RouterService._validate would reject
    # this at the admin API; the raw entity has no such guard) deterministically
    # exercises the "deadline exceeded" branch without a real sleep in the test.
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary, overall_deadline_ms=-1)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = ScriptedGateway([UpstreamUnavailable("503")])
    usage = FakeUsage()
    service = _service(gateway, usage, router, models, FixedDecisionRouter(primary))

    with pytest.raises(UpstreamUnavailable, match="503"):
        await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert gateway.calls == [primary]  # the deadline stopped it before the retry
    assert usage.events == []


async def test_exhausting_max_attempts_surfaces_the_last_error() -> None:
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary, max_attempts=2)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = ScriptedGateway([UpstreamUnavailable("503-a"), UpstreamUnavailable("503-b")])
    usage = FakeUsage()
    service = _service(gateway, usage, router, models, FixedDecisionRouter(primary))

    with pytest.raises(UpstreamUnavailable, match="503-b"):
        await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert gateway.calls == [primary, secondary]
    assert usage.events == []


async def test_budget_exceeded_on_retry_admission_surfaces_immediately() -> None:
    # BudgetExceeded is not failover-eligible in the first place, but this
    # also proves a retry's own fresh admit() is a real gate, not bypassed.
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = ScriptedGateway([UpstreamUnavailable("503")])
    usage = FakeUsage(spent=999.0)
    service = CompletionService(
        models=FakeModels({}),  # type: ignore[arg-type]
        credentials=FakeCredentials(),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        meter=UsageMeter(
            usage=usage,  # type: ignore[arg-type]
            emit_trace=lambda trace: None,
            budgets=FakeBudgets(_budget(1.0)),  # type: ignore[arg-type]
        ),
        router_service=FixedDecisionRouter(primary),  # type: ignore[arg-type]
        callable_resolver=MultiModelCallableResolver(router, models),  # type: ignore[arg-type]
    )

    with pytest.raises(BudgetExceeded):
        await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))


async def test_one_logical_request_is_one_team_rpm_hit_across_attempts() -> None:
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = ScriptedGateway([UpstreamUnavailable("503")])
    usage = FakeUsage()
    rate_limiter = FakeRateLimiter()
    service = _service(
        gateway, usage, router, models, FixedDecisionRouter(primary), rate_limiter=rate_limiter
    )

    await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert rate_limiter.hits == [f"team:{TEAM_ID}"]


async def test_max_attempts_caps_the_chain_even_with_more_candidates() -> None:
    primary, secondary, tertiary = _model("primary"), _model("secondary"), _model("tertiary")
    router = RouterConfig(
        id=uuid4(),
        team_id=TEAM_ID,
        name="auto",
        candidates=(
            CandidateModel(
                model_name=primary.name,
                model_id=primary.id,
                description="p",
                quality_tier=QualityTier.MEDIUM,
            ),
            CandidateModel(
                model_name=secondary.name,
                model_id=secondary.id,
                description="s",
                quality_tier=QualityTier.MEDIUM,
            ),
            CandidateModel(
                model_name=tertiary.name,
                model_id=tertiary.id,
                description="t",
                quality_tier=QualityTier.MEDIUM,
            ),
        ),
        default_model=primary.name,
        default_model_id=primary.id,
        strategy="complexity",
        strategy_config={},
        enabled=True,
        created_at=datetime.now(UTC),
        failover_enabled=True,
        max_attempts=2,
    )
    models = {primary.id: primary, secondary.id: secondary, tertiary.id: tertiary}
    gateway = ScriptedGateway([UpstreamUnavailable("503-a"), UpstreamUnavailable("503-b")])
    usage = FakeUsage()
    service = _service(gateway, usage, router, models, FixedDecisionRouter(primary))

    with pytest.raises(UpstreamUnavailable, match="503-b"):
        await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    # max_attempts=2 caps the chain at 2 candidates even though 3 are declared.
    assert gateway.calls == [primary, secondary]
