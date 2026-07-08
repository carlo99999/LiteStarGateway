"""Unit tests for the real MLflow trace sink, with a fake MlflowClient.

`mlflow` is pinned open-ended (`>=3.14`, no upper bound), so an SDK signature
change would break prod tracing with no CI signal. These tests pin the exact
client calls `MLflowTraceSink` makes (start_trace/end_trace, experiment
resolution, status mapping) against a fake client, mirroring the fake-client
pattern already used for `MlflowMetricsPublisher` in test_mlflow_metrics.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from litestar_gateway.domain.entities import TraceRecord
from litestar_gateway.infrastructure.observability.mlflow_sink import MLflowTraceSink


@dataclass
class _Span:
    trace_id: str


@dataclass
class _Experiment:
    experiment_id: str


class FakeMlflowClient:
    """Records the MLflow client calls the trace sink makes."""

    def __init__(self, existing_experiment_id: str | None = None) -> None:
        self._existing = existing_experiment_id
        self.created_experiments: list[str] = []
        self.get_by_name_calls: list[str] = []
        self.started: list[dict[str, Any]] = []
        self.ended: list[tuple[str, str]] = []  # (trace_id, status)
        self._next_trace = 0

    def get_experiment_by_name(self, name: str) -> _Experiment | None:
        self.get_by_name_calls.append(name)
        return _Experiment(self._existing) if self._existing is not None else None

    def create_experiment(self, name: str) -> str:
        self.created_experiments.append(name)
        return "exp-created"

    def start_trace(
        self, *, name: str, attributes: dict[str, str], tags: dict[str, str], experiment_id: str
    ) -> _Span:
        self.started.append(
            {
                "name": name,
                "attributes": attributes,
                "tags": tags,
                "experiment_id": experiment_id,
            }
        )
        self._next_trace += 1
        return _Span(trace_id=f"trace-{self._next_trace}")

    def end_trace(self, trace_id: str, status: str) -> None:
        self.ended.append((trace_id, status))


def _record(status: str = "ok", error_type: str | None = None) -> TraceRecord:
    return TraceRecord(
        team_id=uuid4(),
        api_key_id=uuid4(),
        model_name="gpt-4o",
        provider="openai",
        operation="chat.completions",
        prompt_tokens=5,
        completion_tokens=7,
        cost=0.02,
        latency_ms=123.0,
        status=status,
        created_at=datetime.now(UTC),
        error_type=error_type,
    )


def test_write_creates_experiment_and_emits_ok_trace() -> None:
    client = FakeMlflowClient(existing_experiment_id=None)
    sink = MLflowTraceSink("http://mlflow:5000", "gateway", client=client)
    record = _record()

    sink.write(record)

    # No experiment existed → it is created, then used as the trace's experiment.
    assert client.created_experiments == ["gateway"]
    assert len(client.started) == 1
    started = client.started[0]
    assert started["name"] == "chat.completions"
    assert started["experiment_id"] == "exp-created"
    assert started["tags"] == {"operation": "chat.completions", "status": "ok"}
    # Billing/observability attributes are all present and stringified.
    attrs = started["attributes"]
    assert attrs["team_id"] == str(record.team_id)
    assert attrs["api_key_id"] == str(record.api_key_id)
    assert attrs["model"] == "gpt-4o"
    assert attrs["provider"] == "openai"
    assert attrs["prompt_tokens"] == "5"
    assert attrs["completion_tokens"] == "7"
    assert attrs["cost"] == "0.02"
    assert attrs["latency_ms"] == "123.0"
    assert "error_type" not in attrs  # only set on error traces
    # Status maps to MLflow's OK on a successful call.
    assert client.ended == [("trace-1", "OK")]


def test_write_maps_error_status_and_records_error_type() -> None:
    client = FakeMlflowClient(existing_experiment_id="exp-existing")
    sink = MLflowTraceSink("http://mlflow:5000", "gateway", client=client)

    sink.write(_record(status="error", error_type="UpstreamTimeout"))

    # Existing experiment is reused; nothing is created.
    assert client.created_experiments == []
    assert client.started[0]["experiment_id"] == "exp-existing"
    assert client.started[0]["tags"]["status"] == "error"
    assert client.started[0]["attributes"]["error_type"] == "UpstreamTimeout"
    # Any non-"ok" status maps to MLflow's ERROR.
    assert client.ended == [("trace-1", "ERROR")]


def test_experiment_resolved_once_across_writes() -> None:
    # The experiment id is cached: repeated writes must not re-query/re-create it.
    client = FakeMlflowClient(existing_experiment_id=None)
    sink = MLflowTraceSink("http://mlflow:5000", "gateway", client=client)

    sink.write(_record())
    sink.write(_record())

    assert client.get_by_name_calls == ["gateway"]  # resolved exactly once
    assert client.created_experiments == ["gateway"]
    assert len(client.started) == 2
