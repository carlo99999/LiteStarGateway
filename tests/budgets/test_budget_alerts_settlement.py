"""Plan 07 Phase 1 — evaluate proactive budget alerts at settlement.

Integration tests through `UsageMeter.settle_ok` with fake ports: a threshold
newly crossed by committed spend must record its dedup key and enqueue exactly
one outbox row; a threshold already fired for the current period must not
enqueue again; alert evaluation must never break the settlement itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

import litestar_gateway.application.usage_meter as usage_meter_module
from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.entities import (
    Budget,
    BudgetAlertState,
    BudgetWindow,
    Model,
    ModelType,
    PendingBudgetAlert,
    Provider,
    TraceRecord,
    UsageEvent,
)
from litestar_gateway.infrastructure.budget_reservation import (
    InMemoryBudgetReservationStore,
)

TEAM_ID = uuid4()
KEY_ID = uuid4()


def _model(input_cost: float = 1.0, output_cost: float = 0.0) -> Model:
    return Model(
        id=uuid4(),
        team_id=TEAM_ID,
        name="m",
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gpt-4o",
        params={},
        params_enforced={},
        api_version=None,
        input_cost_per_token=input_cost,
        output_cost_per_token=output_cost,
        enabled=True,
        created_at=datetime.now(UTC),
    )


def _budget(
    limit: float,
    thresholds: list[int],
    window: BudgetWindow = BudgetWindow.MONTHLY,
) -> Budget:
    return Budget(
        id=uuid4(),
        team_id=TEAM_ID,
        limit_cost=limit,
        window=window,
        created_at=datetime.now(UTC),
        thresholds=thresholds,
    )


class FakeUsage:
    """Accumulates real events so `spend_since` reflects committed cost,
    mirroring the SQL aggregate the real repository computes."""

    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    async def record(self, event: UsageEvent) -> None:
        self.events.append(event)

    async def enqueue_pending(self, event: UsageEvent) -> None:  # pragma: no cover
        raise AssertionError("outbox must not be used in these tests")

    async def spend_since(self, team_id: UUID, since: datetime) -> float:
        return sum(e.cost for e in self.events if e.team_id == team_id and e.created_at >= since)


class FakeBudgets:
    def __init__(self, budget: Budget | None) -> None:
        self._budget = budget

    async def get(self, team_id: UUID) -> Budget | None:
        return self._budget if self._budget and self._budget.team_id == team_id else None


class FakeBudgetAlertState:
    """In-memory dedup ledger + outbox, mirroring the real repository's
    contract: `record_fired_and_enqueue` returns `None` on a duplicate dedup
    key, and writes both artifacts or neither."""

    def __init__(self) -> None:
        self._fired: set[tuple[UUID, BudgetWindow, datetime, int]] = set()
        self.enqueued: list[PendingBudgetAlert] = []
        self.record_fired_calls = 0
        self.raise_on_fired_thresholds: Exception | None = None
        self.raise_on_write: Exception | None = None

    async def fired_thresholds(
        self, team_id: UUID, window: BudgetWindow, period_start: datetime
    ) -> set[int]:
        if self.raise_on_fired_thresholds is not None:
            raise self.raise_on_fired_thresholds
        return {
            t
            for (tid, w, ps, t) in self._fired
            if tid == team_id and w == window and ps == period_start
        }

    async def record_fired_and_enqueue(self, alert: PendingBudgetAlert) -> BudgetAlertState | None:
        self.record_fired_calls += 1
        key = (alert.team_id, alert.window, alert.period_start, alert.threshold)
        if key in self._fired:
            return None
        if self.raise_on_write is not None:
            # A real transaction rolls back both inserts, so the fake must not
            # leave the dedup key behind either.
            raise self.raise_on_write
        self._fired.add(key)
        self.enqueued.append(alert)
        return BudgetAlertState(
            id=uuid4(),
            team_id=alert.team_id,
            window=alert.window,
            period_start=alert.period_start,
            threshold=alert.threshold,
            fired_at=datetime.now(UTC),
        )

    async def pending_alerts(self, *, limit: int = 50) -> list[PendingBudgetAlert]:
        return list(self.enqueued[:limit])


def _freeze(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    """Freeze `datetime.now(UTC)` as read inside `usage_meter.py`, so tests can
    deterministically control which budget period a settlement lands in
    without changing the production code's time source."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]  # noqa: ARG003
            return moment

    monkeypatch.setattr(usage_meter_module, "datetime", _Frozen)


def _meter(
    usage: FakeUsage, budgets: FakeBudgets | None, alert_state: FakeBudgetAlertState | None
) -> UsageMeter:
    traces: list[TraceRecord] = []
    return UsageMeter(
        usage=usage,  # type: ignore[arg-type]
        emit_trace=traces.append,
        budgets=budgets,  # type: ignore[arg-type]
        reservations=InMemoryBudgetReservationStore(),
        budget_alert_state=alert_state,  # type: ignore[arg-type]
    )


async def _settle(meter: UsageMeter, model: Model, prompt_tokens: int) -> None:
    await meter.settle_ok(
        TEAM_ID,
        KEY_ID,
        model,
        "chat",
        {"usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 0}},
        latency_ms=1.0,
    )


async def test_crossing_80_percent_enqueues_exactly_one_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze(monkeypatch, datetime(2026, 7, 15, tzinfo=UTC))
    model = _model()
    alert_state = FakeBudgetAlertState()
    meter = _meter(FakeUsage(), FakeBudgets(_budget(100.0, [50, 80, 100])), alert_state)

    await _settle(meter, model, prompt_tokens=85)

    assert [a.threshold for a in alert_state.enqueued] == [50, 80]


async def test_subsequent_settlement_still_above_threshold_enqueues_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze(monkeypatch, datetime(2026, 7, 15, tzinfo=UTC))
    model = _model()
    usage = FakeUsage()
    alert_state = FakeBudgetAlertState()
    meter = _meter(usage, FakeBudgets(_budget(100.0, [50, 80, 100])), alert_state)

    await _settle(meter, model, prompt_tokens=85)
    assert len(alert_state.enqueued) == 2

    # A second settlement still >= 80% (and < 100%) of the same period.
    await _settle(meter, model, prompt_tokens=1)

    assert len(alert_state.enqueued) == 2  # nothing new enqueued


async def test_multiple_thresholds_crossed_in_one_settlement_both_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze(monkeypatch, datetime(2026, 7, 15, tzinfo=UTC))
    model = _model()
    alert_state = FakeBudgetAlertState()
    meter = _meter(FakeUsage(), FakeBudgets(_budget(100.0, [50, 80, 100])), alert_state)

    # Spend jumps from 0% to 95% in one settlement: 50 and 80 both cross.
    await _settle(meter, model, prompt_tokens=95)

    assert [a.threshold for a in alert_state.enqueued] == [50, 80]


async def test_period_rollover_rearms_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _model()
    usage = FakeUsage()
    alert_state = FakeBudgetAlertState()
    meter = _meter(usage, FakeBudgets(_budget(100.0, [80])), alert_state)

    _freeze(monkeypatch, datetime(2026, 7, 15, tzinfo=UTC))
    await _settle(meter, model, prompt_tokens=85)
    assert [a.threshold for a in alert_state.enqueued] == [80]

    # Spend resets for the new monthly period (a real repository's
    # spend_since would reflect this since it filters on created_at); the
    # fake tracks all-time events, so simulate the reset directly.
    usage.events.clear()

    _freeze(monkeypatch, datetime(2026, 8, 15, tzinfo=UTC))
    await _settle(meter, model, prompt_tokens=85)

    assert [a.threshold for a in alert_state.enqueued] == [80, 80]


async def test_budget_with_no_configured_thresholds_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze(monkeypatch, datetime(2026, 7, 15, tzinfo=UTC))
    model = _model()
    alert_state = FakeBudgetAlertState()
    meter = _meter(FakeUsage(), FakeBudgets(_budget(100.0, [])), alert_state)

    await _settle(meter, model, prompt_tokens=95)

    assert alert_state.enqueued == []
    assert alert_state.record_fired_calls == 0


async def test_no_budget_configured_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _freeze(monkeypatch, datetime(2026, 7, 15, tzinfo=UTC))
    model = _model()
    alert_state = FakeBudgetAlertState()
    meter = _meter(FakeUsage(), FakeBudgets(None), alert_state)

    await _settle(meter, model, prompt_tokens=95)

    assert alert_state.enqueued == []


async def test_no_alert_state_port_wired_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    _freeze(monkeypatch, datetime(2026, 7, 15, tzinfo=UTC))
    model = _model()
    usage = FakeUsage()
    meter = _meter(usage, FakeBudgets(_budget(100.0, [50])), None)

    await _settle(meter, model, prompt_tokens=95)  # must not raise

    assert usage.events  # the ledger write itself still happened


async def test_dedup_key_and_outbox_row_commit_together(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert both artifacts of a fired threshold exist after settlement: the
    dedup ledger entry and the outbox row, with matching identifying fields."""
    _freeze(monkeypatch, datetime(2026, 7, 15, tzinfo=UTC))
    model = _model()
    budget = _budget(100.0, [80])
    alert_state = FakeBudgetAlertState()
    meter = _meter(FakeUsage(), FakeBudgets(budget), alert_state)

    await _settle(meter, model, prompt_tokens=85)

    fired = await alert_state.fired_thresholds(
        TEAM_ID, budget.window, datetime(2026, 7, 1, tzinfo=UTC)
    )
    assert fired == {80}
    assert len(alert_state.enqueued) == 1
    alert = alert_state.enqueued[0]
    assert alert.team_id == TEAM_ID
    assert alert.window == budget.window
    assert alert.threshold == 80
    assert alert.period_start == datetime(2026, 7, 1, tzinfo=UTC)


async def test_concurrent_settlement_race_skips_duplicate_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent settlement (simulated by pre-seeding the dedup ledger, as
    if another replica's `record_fired` already won the unique-constraint
    race) must not enqueue a second alert for the same threshold."""
    _freeze(monkeypatch, datetime(2026, 7, 15, tzinfo=UTC))
    model = _model()
    alert_state = FakeBudgetAlertState()
    # Pre-seed as if a concurrent settlement already fired the 80% threshold.
    alert_state._fired.add((TEAM_ID, BudgetWindow.MONTHLY, datetime(2026, 7, 1, tzinfo=UTC), 80))
    meter = _meter(FakeUsage(), FakeBudgets(_budget(100.0, [80])), alert_state)

    await _settle(meter, model, prompt_tokens=85)

    # crossed_thresholds excludes the pre-seeded 80 from `fired`, so
    # record_fired is never even called for it.
    assert alert_state.enqueued == []


async def test_alert_evaluation_failure_does_not_break_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken alert-evaluation dependency must never fail the request nor
    prevent the actual billing settlement from landing."""
    _freeze(monkeypatch, datetime(2026, 7, 15, tzinfo=UTC))
    model = _model()
    usage = FakeUsage()
    alert_state = FakeBudgetAlertState()
    alert_state.raise_on_fired_thresholds = RuntimeError("dedup ledger unavailable")
    meter = _meter(usage, FakeBudgets(_budget(100.0, [80])), alert_state)

    await _settle(meter, model, prompt_tokens=85)  # must not raise

    assert usage.events  # billing still landed despite the alert failure
    assert alert_state.enqueued == []


async def test_a_failed_outbox_write_leaves_the_threshold_unfired_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISSUE-026, first window: the dedup row and the outbox row commit
    together, so a failure leaves NEITHER. Previously the dedup row committed
    first, and a failure right after it marked the threshold fired with nothing
    to deliver — every later evaluation then skipped it, losing the alert."""
    _freeze(monkeypatch, datetime(2026, 7, 15, tzinfo=UTC))
    model = _model()
    budget = _budget(100.0, [80])
    alert_state = FakeBudgetAlertState()
    alert_state.raise_on_write = RuntimeError("outbox insert failed")
    usage = FakeUsage()
    meter = _meter(usage, FakeBudgets(budget), alert_state)

    await _settle(meter, model, prompt_tokens=85)

    period_start = datetime(2026, 7, 1, tzinfo=UTC)
    assert await alert_state.fired_thresholds(TEAM_ID, budget.window, period_start) == set()
    assert alert_state.enqueued == []

    # The next settlement still sees the threshold as un-fired and retries it.
    alert_state.raise_on_write = None
    await _settle(meter, model, prompt_tokens=1)

    assert await alert_state.fired_thresholds(TEAM_ID, budget.window, period_start) == {80}
    assert len(alert_state.enqueued) == 1
