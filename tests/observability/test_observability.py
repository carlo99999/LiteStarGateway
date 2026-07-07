"""Tests for the observability pipeline (dispatcher + sink factory)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from litestar_gateway.config import Settings
from litestar_gateway.domain.entities import TraceRecord
from litestar_gateway.infrastructure.observability.dispatcher import TraceDispatcher
from litestar_gateway.infrastructure.observability.factory import build_trace_sink
from litestar_gateway.infrastructure.observability.null_sink import NullTraceSink


def _record() -> TraceRecord:
    return TraceRecord(
        team_id=uuid4(),
        api_key_id=uuid4(),
        model_name="m",
        provider="openai",
        operation="chat.completions",
        prompt_tokens=1,
        completion_tokens=2,
        cost=0.0,
        latency_ms=12.3,
        status="ok",
        created_at=datetime.now(UTC),
    )


class _FakeSink:
    def __init__(self) -> None:
        self.written: list[TraceRecord] = []

    def write(self, record: TraceRecord) -> None:
        self.written.append(record)


async def test_dispatcher_delivers_to_sink() -> None:
    sink = _FakeSink()
    dispatcher = TraceDispatcher(sink)
    record = _record()
    async with dispatcher.run(None):  # type: ignore[arg-type]
        dispatcher.enqueue(record)
        for _ in range(50):  # let the worker drain
            if sink.written:
                break
            await asyncio.sleep(0.01)
    assert sink.written == [record]


async def test_dispatcher_drops_when_full_without_raising() -> None:
    # No worker draining + maxsize 1: the second enqueue is dropped, not raised.
    dispatcher = TraceDispatcher(_FakeSink(), maxsize=1)
    dispatcher.enqueue(_record())
    dispatcher.enqueue(_record())  # must not raise


def _settings(tracking_uri: str | None) -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        admin_email="a@b.com",
        master_key="m",
        jwt_secret="x" * 40,
        salt_key="s",
        mlflow_tracking_uri=tracking_uri,
    )


def test_factory_returns_null_sink_without_tracking_uri() -> None:
    assert isinstance(build_trace_sink(_settings(None)), NullTraceSink)


def test_factory_returns_mlflow_sink_with_tracking_uri() -> None:
    # Constructing the MLflow sink must not connect (experiment is resolved lazily).
    sink = build_trace_sink(_settings("sqlite:///:memory:"))
    assert type(sink).__name__ == "MLflowTraceSink"
