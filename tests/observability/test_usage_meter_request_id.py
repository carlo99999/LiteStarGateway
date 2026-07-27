"""UsageMeter emits TraceRecord/UsageEvent tagged with the bound request id
(Plan 11 Slice A) — the contextvar-based propagation `current_request_id()`
relies on, exercised directly against the collaborator (no HTTP app needed)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import structlog

from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.entities import (
    ApiKeySpend,
    Model,
    Provider,
    TraceRecord,
    UsageAggregate,
    UsageBucket,
    UsageEvent,
)
from litestar_gateway.domain.entities.enums import ModelType


class _FakeUsageRepository:
    """Minimal stand-in for `UsageRepository`; only `record` is exercised."""

    def __init__(self) -> None:
        self.recorded: list[UsageEvent] = []

    async def record(self, event: UsageEvent) -> None:
        self.recorded.append(event)

    async def list_events(self, team_id: UUID, **_: Any) -> list[UsageEvent]:
        return []

    async def aggregate(self, team_id: UUID, **_: Any) -> list[UsageAggregate]:
        return []

    async def spend_by_api_key(self, team_id: UUID) -> list[ApiKeySpend]:
        return []

    async def spend_since(self, team_id: UUID, since: datetime) -> float:
        return 0.0

    async def enqueue_pending(self, event: UsageEvent) -> None:
        raise AssertionError("outbox must not be used in this test")

    async def reconcile_pending(self, *, limit: int = 100) -> int:
        return 0

    async def cache_savings(self, team_id: UUID) -> tuple[float, int, int, int]:
        return (0.0, 0, 0, 0)

    async def platform_cache_savings(self) -> tuple[float, int, int, int]:
        return (0.0, 0, 0, 0)

    async def timeseries(self, team_id: UUID, **_: Any) -> list[UsageBucket]:
        return []


def _model(team_id: UUID) -> Model:
    return Model(
        id=uuid4(),
        team_id=team_id,
        name="gpt-4o",
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gpt-4o",
        params={},
        api_version=None,
        input_cost_per_token=0.0,
        output_cost_per_token=0.0,
        enabled=True,
        created_at=datetime.now(UTC),
    )


async def test_settle_ok_tags_trace_and_usage_event_with_the_bound_request_id() -> None:
    usage_repo = _FakeUsageRepository()
    traces: list[TraceRecord] = []
    meter = UsageMeter(usage_repo, traces.append)
    team_id = uuid4()
    model = _model(team_id)

    with structlog.contextvars.bound_contextvars(request_id="corr-usage-1"):
        await meter.settle_ok(
            team_id=team_id,
            api_key_id=None,
            model=model,
            operation="chat.completions",
            response={"usage": {"prompt_tokens": 3, "completion_tokens": 5}},
            latency_ms=12.0,
        )

    assert len(usage_repo.recorded) == 1
    assert usage_repo.recorded[0].request_id == "corr-usage-1"
    assert len(traces) == 1
    assert traces[0].request_id == "corr-usage-1"


async def test_settle_ok_outside_a_request_context_tags_nothing() -> None:
    usage_repo = _FakeUsageRepository()
    traces: list[TraceRecord] = []
    meter = UsageMeter(usage_repo, traces.append)
    team_id = uuid4()
    model = _model(team_id)

    await meter.settle_ok(
        team_id=team_id,
        api_key_id=None,
        model=model,
        operation="chat.completions",
        response={"usage": {"prompt_tokens": 3, "completion_tokens": 5}},
        latency_ms=12.0,
    )

    assert usage_repo.recorded[0].request_id is None
    assert traces[0].request_id is None
