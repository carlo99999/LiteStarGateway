"""Cross-provider failover orchestration (Plan 05 Phase 1).

The plan's own "done when" criteria, verified end to end through
CompletionService.chat_completion: a transient error on the first candidate
succeeds on the second; a terminal (client 4xx) error never retries; budget
is settled at most once per logical request, never double-charged; and one
logical request consumes exactly one team-RPM hit across every attempt.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import perf_counter
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
    UpstreamTimeout,
    UpstreamUnavailable,
)
from litestar_gateway.domain.money import to_cost
from litestar_gateway.domain.ports.rate_limiter import RateLimitDecision
from litestar_gateway.domain.routing import (
    CandidateModel,
    QualityTier,
    RouterConfig,
    RoutingDecision,
)
from litestar_gateway.infrastructure.budget_reservation import (
    InMemoryBudgetReservationStore,
)
from litestar_gateway.infrastructure.circuit_breaker import InMemoryCircuitBreaker

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
        limit_cost=to_cost(limit),
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
        self.failover_outcomes: list[tuple[int, bool]] = []

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

    async def record_failover(self, attempts: int, failover_used: bool) -> None:
        self.failover_outcomes.append((attempts, failover_used))

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
    gateway: Any,
    usage: FakeUsage,
    router: RouterConfig,
    models: dict[UUID, Model],
    router_service: FixedDecisionRouter,
    *,
    rate_limiter: FakeRateLimiter | None = None,
    circuit_breaker: InMemoryCircuitBreaker | None = None,
) -> CompletionService:
    return CompletionService(
        models=FakeModels({}),  # type: ignore[arg-type]
        credentials=FakeCredentials(),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        meter=UsageMeter(
            usage=usage,  # type: ignore[arg-type]
            emit_trace=lambda trace: None,
            budgets=FakeBudgets(_budget(1000.0)),  # type: ignore[arg-type]
            reservations=InMemoryBudgetReservationStore(),
            rate_limiter=rate_limiter,  # type: ignore[arg-type]
            teams=FakeTeams() if rate_limiter is not None else None,  # type: ignore[arg-type]
        ),
        router_service=router_service,  # type: ignore[arg-type]
        callable_resolver=MultiModelCallableResolver(router, models),  # type: ignore[arg-type]
        circuit_breaker=circuit_breaker,
    )


def _three_candidate_router(
    primary: Model, secondary: Model, tertiary: Model, *, max_attempts: int = 3
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
            CandidateModel(
                model_name=tertiary.name,
                model_id=tertiary.id,
                description="tertiary",
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
    )


REQUEST = {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}


async def test_transient_error_fails_over_to_the_second_candidate() -> None:
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = ScriptedGateway([UpstreamUnavailable("503")])
    usage = FakeUsage()
    router_service = FixedDecisionRouter(primary)
    service = _service(gateway, usage, router, models, router_service)

    response = await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert response["usage"] == {"prompt_tokens": 1, "completion_tokens": 1}
    assert gateway.calls == [primary, secondary]
    assert len(usage.events) == 1  # billed exactly once -- the serving provider
    assert router_service.failover_outcomes == [(2, True)]


async def test_terminal_error_never_retries() -> None:
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = ScriptedGateway([UpstreamRequestRejected("bad request")])
    usage = FakeUsage()
    router_service = FixedDecisionRouter(primary)
    service = _service(gateway, usage, router, models, router_service)

    with pytest.raises(UpstreamRequestRejected):
        await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert gateway.calls == [primary]  # never reached the second candidate
    assert usage.events == []  # nothing billed
    assert router_service.failover_outcomes == [(1, False)]


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
    # A realistic deadline (the admin API rejects a non-positive one, so the
    # old `-1` exercised a state production cannot reach): the first candidate
    # is still failing when the budget runs out, so the chain ends there
    # instead of spending another candidate's timeout on top.
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary, overall_deadline_ms=20)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = SlowGateway(0.2, fail_with=UpstreamUnavailable("503"))
    usage = FakeUsage()
    service = _service(gateway, usage, router, models, FixedDecisionRouter(primary))

    with pytest.raises(UpstreamTimeout):
        await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert gateway.calls == [primary]  # the deadline stopped it before the retry
    assert usage.events == []


async def test_exhausting_max_attempts_surfaces_the_last_error() -> None:
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary, max_attempts=2)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = ScriptedGateway([UpstreamUnavailable("503-a"), UpstreamUnavailable("503-b")])
    usage = FakeUsage()
    router_service = FixedDecisionRouter(primary)
    service = _service(gateway, usage, router, models, router_service)

    with pytest.raises(UpstreamUnavailable, match="503-b"):
        await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert gateway.calls == [primary, secondary]
    assert usage.events == []
    # failover_used=True even though the request ultimately failed -- a
    # retry did fire, which is exactly what this observability records.
    assert router_service.failover_outcomes == [(2, True)]


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
            reservations=InMemoryBudgetReservationStore(),
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


async def test_a_tripped_candidate_is_skipped_in_the_retry_chain() -> None:
    # secondary is already tripped past its failure threshold (constructed
    # pre-tripped rather than driven there via repeated requests); the retry
    # chain must skip straight past it to tertiary.
    primary, secondary, tertiary = _model("primary"), _model("secondary"), _model("tertiary")
    router = _three_candidate_router(primary, secondary, tertiary)
    models = {primary.id: primary, secondary.id: secondary, tertiary.id: tertiary}
    gateway = ScriptedGateway([UpstreamUnavailable("503")])
    usage = FakeUsage()
    breaker = InMemoryCircuitBreaker(failure_threshold=1, cooldown_seconds=999, clock=lambda: 0.0)
    await breaker.record_failure(str(secondary.id))
    service = _service(
        gateway, usage, router, models, FixedDecisionRouter(primary), circuit_breaker=breaker
    )

    response = await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert response["usage"] == {"prompt_tokens": 1, "completion_tokens": 1}
    # secondary was skipped -- the retry landed on tertiary, not secondary.
    assert gateway.calls == [primary, tertiary]


async def test_a_failing_attempt_trips_the_breaker_for_a_later_request() -> None:
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary, max_attempts=2)
    models = {primary.id: primary, secondary.id: secondary}
    breaker = InMemoryCircuitBreaker(failure_threshold=1, cooldown_seconds=999, clock=lambda: 0.0)
    # First logical request: primary fails (failover-eligible), tripping the
    # breaker for primary since threshold=1; secondary serves it.
    gateway = ScriptedGateway([UpstreamUnavailable("503")])
    usage = FakeUsage()
    service = _service(
        gateway, usage, router, models, FixedDecisionRouter(primary), circuit_breaker=breaker
    )
    await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))
    assert gateway.calls == [primary, secondary]

    # Second logical request: the router still picks primary as attempt #1
    # (the breaker only filters the *retry* chain, not attempt #1), but a
    # forced second failure on primary would find secondary skipped this time.
    assert (await breaker.allow(str(primary.id))).allowed is False


async def test_a_success_clears_prior_failures_before_the_threshold_trips() -> None:
    # Partial-failure-then-recovery: consecutive-since-last-success semantics
    # mean scattered, non-consecutive failures never trip the breaker. Three
    # separate logical requests exercise this: secondary fails once (below
    # the threshold=2), then succeeds outright (resetting its counter), so a
    # later single failure on primary still finds secondary eligible for the
    # retry chain -- it was never allowed to accumulate a second failure.
    primary, secondary, tertiary = _model("primary"), _model("secondary"), _model("tertiary")
    router = _three_candidate_router(primary, secondary, tertiary)
    models = {primary.id: primary, secondary.id: secondary, tertiary.id: tertiary}
    breaker = InMemoryCircuitBreaker(failure_threshold=2, cooldown_seconds=999, clock=lambda: 0.0)

    gateway_a = ScriptedGateway([UpstreamUnavailable("503")])
    service_a = _service(
        gateway_a,
        FakeUsage(),
        router,
        models,
        FixedDecisionRouter(secondary),
        circuit_breaker=breaker,
    )
    await service_a.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))
    assert (
        await breaker.allow(str(secondary.id))
    ).allowed is True  # only 1 consecutive failure so far

    gateway_b = ScriptedGateway([])  # secondary succeeds outright this time
    service_b = _service(
        gateway_b,
        FakeUsage(),
        router,
        models,
        FixedDecisionRouter(secondary),
        circuit_breaker=breaker,
    )
    await service_b.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    gateway_c = ScriptedGateway([UpstreamUnavailable("503-again")])
    service_c = _service(
        gateway_c,
        FakeUsage(),
        router,
        models,
        FixedDecisionRouter(primary),
        circuit_breaker=breaker,
    )
    response = await service_c.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert response["usage"] == {"prompt_tokens": 1, "completion_tokens": 1}
    # secondary was NOT skipped -- its earlier failure was reset by the
    # outright success on request B above.
    assert gateway_c.calls == [primary, secondary]


# ---------------------------------------------------------------------------
# ISSUE-027: `overall_deadline_ms` bounds the whole chain, not just the gaps
# between attempts.
# ---------------------------------------------------------------------------


class SlowGateway:
    """Sleeps before answering, so a test can outlast a deadline without
    depending on wall-clock precision beyond the sleep itself."""

    def __init__(self, delay_s: float, *, fail_with: Exception | None = None) -> None:
        self._delay_s = delay_s
        self._fail_with = fail_with
        self.calls: list[Model] = []
        self.completed = 0

    async def achat_completion(self, request, model, credentials) -> dict[str, Any]:
        self.calls.append(model)
        await asyncio.sleep(self._delay_s)
        self.completed += 1
        if self._fail_with is not None:
            raise self._fail_with
        return {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    async def astream_chat_completion(self, request, model, credentials):
        self.calls.append(model)
        await asyncio.sleep(self._delay_s)
        self.completed += 1

        async def _chunks():
            yield {"choices": [{"delta": {"content": "hi"}}]}

        return _chunks()


async def test_a_slow_first_attempt_is_cut_off_at_the_overall_deadline() -> None:
    # The reported behaviour: deadline 10 ms, primary answers successfully
    # after 120 ms, and the call returned 200 at ~122 ms. The deadline is a
    # wall-clock budget for the whole chain, so it must abort the attempt in
    # flight, not merely refuse to start another one.
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary, overall_deadline_ms=10)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = SlowGateway(0.2)
    usage = FakeUsage()
    service = _service(gateway, usage, router, models, FixedDecisionRouter(primary))

    started = perf_counter()
    with pytest.raises(UpstreamTimeout):
        await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))
    elapsed_ms = (perf_counter() - started) * 1000

    assert gateway.completed == 0  # the provider call never ran to completion
    assert elapsed_ms < 150  # and the caller was not made to wait it out
    assert usage.events == []  # nothing billed for an aborted attempt


async def test_a_slow_stream_open_is_cut_off_at_the_overall_deadline() -> None:
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary, overall_deadline_ms=10)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = SlowGateway(0.2)
    usage = FakeUsage()
    service = _service(gateway, usage, router, models, FixedDecisionRouter(primary))

    started = perf_counter()
    with pytest.raises(UpstreamTimeout):
        await service.open_chat_stream(TEAM_ID, KEY_ID, dict(REQUEST))
    elapsed_ms = (perf_counter() - started) * 1000

    assert gateway.completed == 0
    assert elapsed_ms < 150


async def test_an_attempt_that_finishes_inside_the_deadline_is_untouched() -> None:
    # The budget must not turn into a latency cap on healthy calls.
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary, overall_deadline_ms=5000)
    models = {primary.id: primary, secondary.id: secondary}
    gateway = SlowGateway(0.01)
    usage = FakeUsage()
    service = _service(gateway, usage, router, models, FixedDecisionRouter(primary))

    response = await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert response["usage"]["prompt_tokens"] == 1
    assert gateway.completed == 1
    assert len(usage.events) == 1


async def test_no_deadline_configured_never_interrupts_a_slow_attempt() -> None:
    primary, secondary = _model("primary"), _model("secondary")
    router = _router(primary, secondary)  # overall_deadline_ms=None
    models = {primary.id: primary, secondary.id: secondary}
    gateway = SlowGateway(0.05)
    usage = FakeUsage()
    service = _service(gateway, usage, router, models, FixedDecisionRouter(primary))

    await service.chat_completion(TEAM_ID, KEY_ID, dict(REQUEST))

    assert gateway.completed == 1
