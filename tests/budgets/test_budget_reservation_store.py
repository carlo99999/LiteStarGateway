"""Conformance suite for `BudgetReservationStore`, run against every adapter.

One suite, not one file per adapter: two implementations with separate tests is
how the Redis circuit breaker drifted from the in-memory one until a real Redis
disagreed with both (ISSUE-029). Everything here is a property of the *port*, so
an adapter that passes is substitutable for the other.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

import pytest
from support.doubles import MutableClock

from litestar_gateway.infrastructure.budget_reservation import (
    InMemoryBudgetReservationStore,
)

TTL = 300


@pytest.fixture(params=["in_memory"])
def store_factory(request: pytest.FixtureRequest) -> Callable[[MutableClock], object]:
    """Builds a store bound to a test-controlled clock, over a backend SHARED by
    every store the test builds — so calling the factory twice models two
    replicas. For the in-memory adapter the shared backend is the object itself
    (one process is one replica); for Redis it is the server.

    Parametrized so every adapter answers the same questions.
    """
    built: dict[int, object] = {}

    def build(clock: MutableClock):
        if id(clock) not in built:
            built[id(clock)] = InMemoryBudgetReservationStore(clock=clock)
        return built[id(clock)]

    return build


async def test_a_request_within_the_cap_is_admitted(store_factory) -> None:
    clock = MutableClock()
    store = store_factory(clock)
    team = uuid4()

    outcome = await store.try_reserve(team, 1.0, spent=0.0, limit=10.0, ttl_s=TTL)

    assert outcome.admitted
    assert outcome.reserved == 0.0
    assert outcome.reservation is not None
    assert outcome.reservation.team_id == team


async def test_in_flight_reservations_count_towards_the_cap(store_factory) -> None:
    # The whole point of the store: committed spend alone would admit both.
    clock = MutableClock()
    store = store_factory(clock)
    team = uuid4()
    first = await store.try_reserve(team, 6.0, spent=0.0, limit=10.0, ttl_s=TTL)
    assert first.admitted

    second = await store.try_reserve(team, 6.0, spent=5.0, limit=10.0, ttl_s=TTL)

    assert not second.admitted
    assert second.reserved == 6.0  # reported for the refusal message


async def test_two_replicas_sharing_one_store_admit_only_one(store_factory) -> None:
    """Two stores over the same backend are two replicas. With room for exactly
    one request, exactly one may pass — this is the property a per-process
    counter cannot provide, and the reason this port exists."""
    clock = MutableClock()
    replica_one, replica_two = store_factory(clock), store_factory(clock)
    team = uuid4()

    first = await replica_one.try_reserve(team, 10.0, spent=0.0, limit=10.0, ttl_s=TTL)
    second = await replica_two.try_reserve(team, 10.0, spent=0.0, limit=10.0, ttl_s=TTL)

    assert [first.admitted, second.admitted].count(True) == 1


async def test_releasing_gives_the_headroom_back(store_factory) -> None:
    clock = MutableClock()
    store = store_factory(clock)
    team = uuid4()
    held = await store.try_reserve(team, 10.0, spent=0.0, limit=10.0, ttl_s=TTL)
    assert held.reservation is not None
    assert not (await store.try_reserve(team, 1.0, spent=0.0, limit=10.0, ttl_s=TTL)).admitted

    await store.release(held.reservation)

    assert (await store.try_reserve(team, 1.0, spent=0.0, limit=10.0, ttl_s=TTL)).admitted


async def test_releasing_twice_is_a_no_op(store_factory) -> None:
    # Identity, not arithmetic: subtracting an amount twice would silently
    # under-count and hand the team free headroom.
    clock = MutableClock()
    store = store_factory(clock)
    team = uuid4()
    held = await store.try_reserve(team, 4.0, spent=0.0, limit=10.0, ttl_s=TTL)
    assert held.reservation is not None

    await store.release(held.reservation)
    await store.release(held.reservation)

    outcome = await store.try_reserve(team, 1.0, spent=0.0, limit=10.0, ttl_s=TTL)
    assert outcome.reserved == 0.0


async def test_an_expired_reservation_stops_holding_headroom(store_factory) -> None:
    """A replica that dies mid-request takes its state with it. Without expiry
    its reservations would hold the team's headroom forever."""
    clock = MutableClock()
    store = store_factory(clock)
    team = uuid4()
    assert (await store.try_reserve(team, 10.0, spent=0.0, limit=10.0, ttl_s=TTL)).admitted

    clock.now += TTL + 1

    outcome = await store.try_reserve(team, 1.0, spent=0.0, limit=10.0, ttl_s=TTL)
    assert outcome.admitted
    assert outcome.reserved == 0.0


async def test_a_live_reservation_is_not_swept_early(store_factory) -> None:
    clock = MutableClock()
    store = store_factory(clock)
    team = uuid4()
    assert (await store.try_reserve(team, 10.0, spent=0.0, limit=10.0, ttl_s=TTL)).admitted

    clock.now += TTL - 1

    assert not (await store.try_reserve(team, 1.0, spent=0.0, limit=10.0, ttl_s=TTL)).admitted


async def test_teams_do_not_share_headroom(store_factory) -> None:
    clock = MutableClock()
    store = store_factory(clock)
    mine, theirs = uuid4(), uuid4()
    assert (await store.try_reserve(mine, 10.0, spent=0.0, limit=10.0, ttl_s=TTL)).admitted

    outcome = await store.try_reserve(theirs, 10.0, spent=0.0, limit=10.0, ttl_s=TTL)

    assert outcome.admitted
    assert outcome.reserved == 0.0


async def test_a_zero_amount_request_still_respects_an_exhausted_cap(store_factory) -> None:
    # Matches the gate's existing semantics: at or over the limit, nothing new
    # is admitted, however cheap it claims to be.
    clock = MutableClock()
    store = store_factory(clock)
    team = uuid4()

    outcome = await store.try_reserve(team, 0.0, spent=10.0, limit=10.0, ttl_s=TTL)

    assert not outcome.admitted
