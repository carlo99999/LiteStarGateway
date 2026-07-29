"""Per-key spend caps: enforcement at admission, and the two modes.

The per-key cap is always a *sub*-limit — the team gate runs regardless — so the
tests worth having are about what the second gate adds: that `block` refuses
before the provider is called, that `alert` does not, that the two pools never
share a reservation bucket, and that both claims are released together.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.entities import (
    ApiKeyBudget,
    Budget,
    BudgetWindow,
    KeyBudgetMode,
    Model,
    ModelType,
    Provider,
    UsageEvent,
)
from litestar_gateway.domain.exceptions import BudgetExceeded
from litestar_gateway.domain.ports.budget_reservation import key_scope, team_scope
from litestar_gateway.infrastructure.budget_reservation import InMemoryBudgetReservationStore

TEAM_ID = uuid4()
KEY_ID = uuid4()
OTHER_KEY_ID = uuid4()


def _model() -> Model:
    return Model(
        id=uuid4(),
        team_id=TEAM_ID,
        name="m",
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gpt-4o",
        params={},
        api_version=None,
        input_cost_per_token=0.001,
        output_cost_per_token=0.001,
        enabled=True,
        created_at=datetime.now(UTC),
    )


class FakeUsage:
    def __init__(self, *, team_spent: str = "0", key_spent: dict[UUID, str] | None = None) -> None:
        self.events: list[UsageEvent] = []
        self._team_spent = Decimal(team_spent)
        self._key_spent = {k: Decimal(v) for k, v in (key_spent or {}).items()}

    async def record(self, event: UsageEvent) -> None:
        self.events.append(event)

    async def enqueue_pending(self, event: UsageEvent) -> None:  # pragma: no cover
        raise AssertionError("outbox must not be used in these tests")

    async def spend_since(self, team_id: UUID, since: datetime) -> Decimal:
        return self._team_spent

    async def key_spend_since(self, api_key_id: UUID, since: datetime) -> Decimal:
        return self._key_spent.get(api_key_id, Decimal(0))


class FakeBudgets:
    def __init__(self, limit: str | None = "1000") -> None:
        self._limit = limit

    async def get(self, team_id: UUID) -> Budget | None:
        if self._limit is None:
            return None
        return Budget(
            id=uuid4(),
            team_id=team_id,
            limit_cost=Decimal(self._limit),
            window=BudgetWindow.MONTHLY,
            created_at=datetime.now(UTC),
        )


class FakeKeyBudgets:
    def __init__(self, budgets: dict[UUID, ApiKeyBudget] | None = None) -> None:
        self._budgets = budgets or {}

    async def get(self, api_key_id: UUID) -> ApiKeyBudget | None:
        return self._budgets.get(api_key_id)

    async def list_for_team(self, team_id: UUID) -> list[ApiKeyBudget]:  # pragma: no cover
        return [b for b in self._budgets.values() if b.team_id == team_id]

    async def set(self, budget: ApiKeyBudget) -> ApiKeyBudget:  # pragma: no cover
        self._budgets[budget.api_key_id] = budget
        return budget

    async def remove(self, api_key_id: UUID) -> bool:  # pragma: no cover
        return self._budgets.pop(api_key_id, None) is not None


def _key_budget(
    limit: str, *, mode: KeyBudgetMode = KeyBudgetMode.BLOCK, key_id: UUID = KEY_ID
) -> ApiKeyBudget:
    return ApiKeyBudget(
        id=uuid4(),
        api_key_id=key_id,
        team_id=TEAM_ID,
        limit_cost=Decimal(limit),
        window=BudgetWindow.MONTHLY,
        mode=mode,
        created_at=datetime.now(UTC),
    )


def _meter(
    usage: FakeUsage,
    *,
    key_budgets: FakeKeyBudgets | None = None,
    team_limit: str | None = "1000",
    store: InMemoryBudgetReservationStore | None = None,
) -> UsageMeter:
    return UsageMeter(
        usage=usage,  # type: ignore[arg-type]
        emit_trace=lambda _: None,
        budgets=FakeBudgets(team_limit),  # type: ignore[arg-type]
        reservations=store or InMemoryBudgetReservationStore(),  # type: ignore[arg-type]
        api_key_budgets=key_budgets,  # type: ignore[arg-type]
    )


REQUEST: dict[str, Any] = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}


async def _reserved(store: InMemoryBudgetReservationStore, scope: str) -> float:
    """The live in-flight total for one scope, read through the store itself."""
    outcome = await store.try_reserve(scope, 0.0, spent=0.0, limit=float("inf"), ttl_s=1)
    if outcome.reservation is not None:
        await store.release(outcome.reservation)
    return outcome.reserved


# ── Off by default ───────────────────────────────────────────────────────────


async def test_without_a_key_budget_admission_is_unchanged() -> None:
    usage = FakeUsage()
    admission = await _meter(usage).admit(TEAM_ID, _model(), REQUEST, api_key_id=KEY_ID)

    assert admission is not None
    # One claim, the team's: nothing about the key was reserved.
    assert len(admission.reservations) == 1
    assert admission.reservations[0].scope == team_scope(TEAM_ID)


async def test_a_call_without_a_key_is_never_gated_per_key() -> None:
    # Internal calls (a router's judge strategy, validation) carry no key.
    usage = FakeUsage(key_spent={KEY_ID: "999"})
    key_budgets = FakeKeyBudgets({KEY_ID: _key_budget("1")})

    admission = await _meter(usage, key_budgets=key_budgets).admit(TEAM_ID, _model(), REQUEST)

    assert admission is not None
    assert [r.scope for r in admission.reservations] == [team_scope(TEAM_ID)]


# ── block mode ───────────────────────────────────────────────────────────────


async def test_block_mode_refuses_a_key_over_its_cap() -> None:
    usage = FakeUsage(key_spent={KEY_ID: "5"})
    key_budgets = FakeKeyBudgets({KEY_ID: _key_budget("5")})

    with pytest.raises(BudgetExceeded, match="API key budget exceeded"):
        await _meter(usage, key_budgets=key_budgets).admit(
            TEAM_ID, _model(), REQUEST, api_key_id=KEY_ID
        )


async def test_the_key_refusal_names_the_key_cap_not_the_team_cap() -> None:
    # The team has plenty of headroom; the actionable message is the key's.
    usage = FakeUsage(team_spent="1", key_spent={KEY_ID: "5"})
    key_budgets = FakeKeyBudgets({KEY_ID: _key_budget("5")})

    with pytest.raises(BudgetExceeded) as exc:
        await _meter(usage, key_budgets=key_budgets).admit(
            TEAM_ID, _model(), REQUEST, api_key_id=KEY_ID
        )

    assert "API key" in str(exc.value)


async def test_a_refused_key_leaves_no_team_reservation_behind() -> None:
    # The key gate runs first precisely so a refusal never has to give a team
    # reservation back — a leaked claim would eat the team's headroom until its
    # TTL expired.
    store = InMemoryBudgetReservationStore()
    usage = FakeUsage(key_spent={KEY_ID: "5"})
    key_budgets = FakeKeyBudgets({KEY_ID: _key_budget("5")})

    with pytest.raises(BudgetExceeded):
        await _meter(usage, key_budgets=key_budgets, store=store).admit(
            TEAM_ID, _model(), REQUEST, api_key_id=KEY_ID
        )

    assert await _reserved(store, team_scope(TEAM_ID)) == 0.0
    assert await _reserved(store, key_scope(KEY_ID)) == 0.0


async def test_block_mode_admits_under_the_cap_and_reserves_both_scopes() -> None:
    store = InMemoryBudgetReservationStore()
    usage = FakeUsage(key_spent={KEY_ID: "1"})
    key_budgets = FakeKeyBudgets({KEY_ID: _key_budget("100")})

    admission = await _meter(usage, key_budgets=key_budgets, store=store).admit(
        TEAM_ID, _model(), REQUEST, api_key_id=KEY_ID
    )

    assert admission is not None
    assert {r.scope for r in admission.reservations} == {
        team_scope(TEAM_ID),
        key_scope(KEY_ID),
    }
    # Two pools, each holding this request's cost — never one shared bucket, or
    # one of the two caps would silently count double.
    assert await _reserved(store, team_scope(TEAM_ID)) > 0.0
    assert await _reserved(store, key_scope(KEY_ID)) > 0.0


async def test_releasing_an_admission_gives_back_every_claim() -> None:
    store = InMemoryBudgetReservationStore()
    usage = FakeUsage()
    key_budgets = FakeKeyBudgets({KEY_ID: _key_budget("100")})
    meter = _meter(usage, key_budgets=key_budgets, store=store)

    admission = await meter.admit(TEAM_ID, _model(), REQUEST, api_key_id=KEY_ID)
    await meter.release(admission)

    assert await _reserved(store, team_scope(TEAM_ID)) == 0.0
    assert await _reserved(store, key_scope(KEY_ID)) == 0.0


async def test_one_keys_in_flight_spend_does_not_bound_another_key() -> None:
    store = InMemoryBudgetReservationStore()
    usage = FakeUsage()
    key_budgets = FakeKeyBudgets(
        {
            KEY_ID: _key_budget("0.0001"),
            OTHER_KEY_ID: _key_budget("100", key_id=OTHER_KEY_ID),
        }
    )
    meter = _meter(usage, key_budgets=key_budgets, store=store)

    # Exhaust the first key's tiny cap with an in-flight reservation.
    await meter.admit(TEAM_ID, _model(), REQUEST, api_key_id=KEY_ID)
    with pytest.raises(BudgetExceeded):
        await meter.admit(TEAM_ID, _model(), REQUEST, api_key_id=KEY_ID)

    # The other key is unaffected: separate scope, separate pool.
    assert await meter.admit(TEAM_ID, _model(), REQUEST, api_key_id=OTHER_KEY_ID) is not None


# ── alert mode ───────────────────────────────────────────────────────────────


async def test_alert_mode_lets_an_over_budget_key_through() -> None:
    # Visibility without the power to break someone's workload: the whole
    # difference between the two modes.
    usage = FakeUsage(key_spent={KEY_ID: "500"})
    key_budgets = FakeKeyBudgets({KEY_ID: _key_budget("5", mode=KeyBudgetMode.ALERT)})

    admission = await _meter(usage, key_budgets=key_budgets).admit(
        TEAM_ID, _model(), REQUEST, api_key_id=KEY_ID
    )

    assert admission is not None
    assert [r.scope for r in admission.reservations] == [team_scope(TEAM_ID)]


async def test_alert_mode_logs_the_overrun(caplog: pytest.LogCaptureFixture) -> None:
    usage = FakeUsage(key_spent={KEY_ID: "500"})
    key_budgets = FakeKeyBudgets({KEY_ID: _key_budget("5", mode=KeyBudgetMode.ALERT)})

    with caplog.at_level("WARNING"):
        await _meter(usage, key_budgets=key_budgets).admit(
            TEAM_ID, _model(), REQUEST, api_key_id=KEY_ID
        )

    assert any("over its budget" in record.message for record in caplog.records)


async def test_alert_mode_under_the_cap_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    usage = FakeUsage(key_spent={KEY_ID: "1"})
    key_budgets = FakeKeyBudgets({KEY_ID: _key_budget("100", mode=KeyBudgetMode.ALERT)})

    with caplog.at_level("WARNING"):
        await _meter(usage, key_budgets=key_budgets).admit(
            TEAM_ID, _model(), REQUEST, api_key_id=KEY_ID
        )

    assert not [r for r in caplog.records if "over its budget" in r.message]


async def test_alert_mode_reserves_nothing_for_the_key() -> None:
    # There is nothing to bound when nothing is being refused, and reserving
    # against a cap that never blocks would only cost a round trip.
    store = InMemoryBudgetReservationStore()
    usage = FakeUsage()
    key_budgets = FakeKeyBudgets({KEY_ID: _key_budget("100", mode=KeyBudgetMode.ALERT)})

    await _meter(usage, key_budgets=key_budgets, store=store).admit(
        TEAM_ID, _model(), REQUEST, api_key_id=KEY_ID
    )

    assert await _reserved(store, key_scope(KEY_ID)) == 0.0


# ── Interaction with the team cap ────────────────────────────────────────────


async def test_the_team_cap_still_binds_a_key_with_a_larger_cap() -> None:
    # A key cap above the team's is harmless: this is why per-key caps can be
    # delegated to whoever issues keys.
    usage = FakeUsage(team_spent="1000")
    key_budgets = FakeKeyBudgets({KEY_ID: _key_budget("999999")})

    with pytest.raises(BudgetExceeded, match="Team budget exceeded"):
        await _meter(usage, key_budgets=key_budgets).admit(
            TEAM_ID, _model(), REQUEST, api_key_id=KEY_ID
        )


async def test_a_key_cap_applies_even_with_no_team_budget_configured() -> None:
    usage = FakeUsage(key_spent={KEY_ID: "5"})
    key_budgets = FakeKeyBudgets({KEY_ID: _key_budget("5")})

    with pytest.raises(BudgetExceeded, match="API key"):
        await _meter(usage, key_budgets=key_budgets, team_limit=None).admit(
            TEAM_ID, _model(), REQUEST, api_key_id=KEY_ID
        )
