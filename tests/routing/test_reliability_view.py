"""The reliability view: retries, failover rate, and live breaker state.

Both inputs already existed — `attempts`/`failover_used` on every persisted
decision, breaker state in Redis — and neither was visible anywhere (Plan 05's
last open item). The property that needed a test rather than a comment: reading
this view must not claim a breaker's half-open trial, because a console that
consumed the retry a real request was owed on every page refresh would be worse
than no view at all.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from support.doubles import FakeRedis, MutableClock  # type: ignore[import-not-found]

from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.domain.ports import BreakerStatus
from litestar_gateway.domain.routing import CandidateModel, QualityTier
from litestar_gateway.infrastructure.circuit_breaker import (
    InMemoryCircuitBreaker,
    RedisCircuitBreaker,
)

TEAM = uuid4()
ROUTER = uuid4()
CHEAP_ID = uuid4()
BIG_ID = uuid4()


class FakeDecisions:
    def __init__(self, rows: list[tuple[int, bool, int]]) -> None:
        self._rows = rows

    async def reliability(self, team_id: UUID, router_id: UUID) -> list[tuple[int, bool, int]]:
        return self._rows


def _router() -> object:
    return type(
        "Router",
        (),
        {
            "id": ROUTER,
            "name": "auto",
            "candidates": (
                CandidateModel(
                    model_name="cheap-model",
                    description="small",
                    quality_tier=QualityTier.SIMPLE,
                    model_id=CHEAP_ID,
                ),
                CandidateModel(
                    model_name="big-model",
                    description="large",
                    quality_tier=QualityTier.COMPLEX,
                    model_id=BIG_ID,
                ),
            ),
        },
    )()


def _service(rows: list[tuple[int, bool, int]]) -> RouterService:
    service = RouterService(
        routers=object(),  # type: ignore[arg-type]
        models=object(),  # type: ignore[arg-type]
        decisions=FakeDecisions(rows),  # type: ignore[arg-type]
    )
    router = _router()

    async def get(team_id: UUID, router_id: UUID):
        return router

    service.get = get  # type: ignore[method-assign]
    return service


# ── The decision half ────────────────────────────────────────────────────────


async def test_attempts_and_failover_rate_are_derived_from_the_decisions() -> None:
    # 900 one-attempt calls, 40 that needed a second provider, 10 a third.
    service = _service([(1, False, 900), (2, True, 40), (3, True, 10)])

    view = await service.reliability(TEAM, ROUTER)

    assert view["total"] == 950
    assert view["by_attempts"] == {"1": 900, "2": 40, "3": 10}
    assert view["failover_used"] == 50
    assert view["failover_rate"] == pytest.approx(50 / 950, abs=1e-4)


async def test_a_router_with_no_traffic_reports_zero_not_a_division_error() -> None:
    view = await service_view([])
    assert (view["total"], view["failover_used"], view["failover_rate"]) == (0, 0, 0.0)
    assert view["by_attempts"] == {}


async def service_view(rows: list[tuple[int, bool, int]]) -> dict:
    return await _service(rows).reliability(TEAM, ROUTER)


async def test_rows_that_share_an_attempt_count_are_summed() -> None:
    # The same attempts value appears twice — once with failover, once without
    # (a retry against the same provider is not a failover). Both are one-and-
    # the-same bucket for "how many attempts did this take".
    view = await service_view([(2, True, 5), (2, False, 3)])

    assert view["by_attempts"] == {"2": 8}
    assert view["failover_used"] == 5


# ── The breaker half ─────────────────────────────────────────────────────────


async def test_candidate_state_is_reported_without_claiming_the_trial() -> None:
    clock = MutableClock(1000.0)
    breaker = InMemoryCircuitBreaker(failure_threshold=1, cooldown_seconds=30, clock=clock)
    await breaker.record_failure(str(BIG_ID))  # trips it
    clock.now += 31  # cooldown elapsed: the next request would get the trial

    view = await _service([(1, False, 1)]).reliability(TEAM, ROUTER, breaker)

    assert [c["breaker"] for c in view["candidates"]] == ["closed", "half_open"]
    # The trial is still there for a real request: reading the view did not take
    # it. Without this the console would consume one retry per refresh.
    assert (await breaker.allow(str(BIG_ID))).trial_token is not None


async def test_an_open_breaker_still_cooling_down_reads_as_open() -> None:
    clock = MutableClock(1000.0)
    breaker = InMemoryCircuitBreaker(failure_threshold=1, cooldown_seconds=30, clock=clock)
    await breaker.record_failure(str(CHEAP_ID))
    clock.now += 5

    view = await _service([]).reliability(TEAM, ROUTER, breaker)

    assert [c["breaker"] for c in view["candidates"]] == ["open", "closed"]


async def test_the_redis_breaker_reports_the_same_states() -> None:
    # The two adapters must agree, or the view means different things depending
    # on whether Redis is wired.
    clock = MutableClock(1000.0)
    breaker = RedisCircuitBreaker(
        FakeRedis(clock=clock), failure_threshold=1, cooldown_seconds=30, clock=clock
    )

    assert await breaker.state(str(BIG_ID)) == "closed"
    await breaker.record_failure(str(BIG_ID))
    assert await breaker.state(str(BIG_ID)) == "open"
    clock.now += 31
    assert await breaker.state(str(BIG_ID)) == "half_open"
    # Still unclaimed after all those reads.
    assert (await breaker.allow(str(BIG_ID))).trial_token is not None


async def test_without_an_inspector_the_decision_half_still_works() -> None:
    # A breaker that cannot answer a read-only query must not make the whole
    # panel unavailable.
    view = await _service([(1, False, 4)]).reliability(TEAM, ROUTER, None)

    assert view["candidates"] == []
    assert view["total"] == 4


async def test_a_candidate_without_a_stable_id_is_omitted() -> None:
    # The breaker is keyed by model id; a pre-revision candidate has none, and
    # reporting it as "closed" would claim knowledge we do not have.
    clock = MutableClock(1000.0)
    breaker = InMemoryCircuitBreaker(failure_threshold=1, cooldown_seconds=30, clock=clock)
    service = _service([])
    router = _router()
    router.candidates = (  # type: ignore[attr-defined]
        CandidateModel(
            model_name="legacy", description="d", quality_tier=QualityTier.SIMPLE, model_id=None
        ),
    )

    async def get(team_id: UUID, router_id: UUID):
        return router

    service.get = get  # type: ignore[method-assign]

    view = await service.reliability(TEAM, ROUTER, breaker)

    assert view["candidates"] == []


def test_the_inspector_capability_is_detected_structurally() -> None:
    # The controller decides by `isinstance` against the protocol, so both real
    # adapters must satisfy it and a breaker without `state` must not.
    from litestar_gateway.domain.ports import CircuitBreakerInspector

    class Bare:
        async def allow(self, key: str) -> None: ...
        async def record_failure(self, key: str, trial_token: str | None = None) -> None: ...
        async def record_success(self, key: str, trial_token: str | None = None) -> None: ...

    assert isinstance(InMemoryCircuitBreaker(1, 30), CircuitBreakerInspector)
    assert isinstance(RedisCircuitBreaker(FakeRedis(), 1, 30), CircuitBreakerInspector)
    assert not isinstance(Bare(), CircuitBreakerInspector)


def test_breaker_status_values_are_the_three_the_state_machine_has() -> None:
    from typing import get_args

    assert set(get_args(BreakerStatus)) == {"closed", "open", "half_open"}
