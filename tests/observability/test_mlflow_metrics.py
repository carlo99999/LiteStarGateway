"""Ops metrics on MLflow: aggregate TraceRecords, publish to a metrics run.

MLflow is this gateway's observability backend (compose-provided or any
MLFLOW_TRACKING_URI). Per-call traces already land there; this adds the
fleet-level RED view: an in-process aggregator (fed by the same TraceRecord
fan-out as the MLflow trace sink) and a background publisher that logs the
totals as time-series metrics to a long-lived "gateway-metrics" run, charted
natively by the MLflow UI next to the traces.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from litestar_gateway.domain.entities import TraceRecord
from litestar_gateway.infrastructure.observability.aggregator import MetricsAggregator
from litestar_gateway.infrastructure.observability.composite import CompositeTraceSink
from litestar_gateway.infrastructure.observability.mlflow_metrics import MlflowMetricsPublisher


def _record(
    status: str = "ok",
    provider: str = "openai",
    latency_ms: float = 100.0,
    error_type: str | None = None,
) -> TraceRecord:
    # Error traces carry zero tokens/cost, exactly like the service's
    # _emit_error_trace (there is nothing billed on a failed call).
    failed = status == "error"
    return TraceRecord(
        team_id=uuid4(),
        api_key_id=uuid4(),
        model_name="m",
        provider=provider,
        operation="chat.completions",
        prompt_tokens=0 if failed else 5,
        completion_tokens=0 if failed else 7,
        cost=0.0 if failed else 0.02,
        latency_ms=latency_ms,
        status=status,
        created_at=datetime.now(UTC),
        error_type=error_type,
    )


class TestAggregator:
    def test_counts_requests_errors_tokens_and_cost(self) -> None:
        agg = MetricsAggregator()
        agg.write(_record(latency_ms=100.0))
        agg.write(_record(latency_ms=300.0))
        agg.write(_record(status="error", error_type="UpstreamTimeout", latency_ms=50.0))

        snap = agg.snapshot()
        assert snap["requests"] == 3
        assert snap["errors"] == 1
        # Tokens/cost are billed only on ok calls (error traces carry zeros).
        assert snap["prompt_tokens"] == 10
        assert snap["completion_tokens"] == 14
        assert round(snap["cost_usd"], 4) == 0.04
        assert snap["latency_ms_avg"] == 150.0  # (100+300+50)/3
        assert snap["latency_ms_max"] == 300.0

    def test_per_provider_breakdown(self) -> None:
        agg = MetricsAggregator()
        agg.write(_record(provider="openai"))
        agg.write(_record(provider="anthropic"))
        agg.write(_record(provider="anthropic", status="error", error_type="X"))

        snap = agg.snapshot()
        assert snap["requests.openai"] == 1
        assert snap["requests.anthropic"] == 2
        assert snap["errors.anthropic"] == 1
        assert "errors.openai" not in snap

    def test_snapshot_keeps_totals_but_resets_interval_stats(self) -> None:
        agg = MetricsAggregator()
        agg.write(_record(latency_ms=200.0))
        first = agg.snapshot()
        assert first["latency_ms_avg"] == 200.0

        second = agg.snapshot()  # no traffic since the last snapshot
        assert second["requests"] == 1  # cumulative counters survive
        assert second["latency_ms_avg"] == 0.0  # interval stats reset
        assert second["latency_ms_max"] == 0.0

    def test_dropped_traces_counter(self) -> None:
        agg = MetricsAggregator()
        agg.record_dropped_trace()
        agg.record_dropped_trace()
        assert agg.snapshot()["dropped_traces"] == 2

    def test_composite_feeds_aggregator_even_if_other_sink_fails(self) -> None:
        class BoomSink:
            def write(self, record: TraceRecord) -> None:
                raise RuntimeError("mlflow down")

        agg = MetricsAggregator()
        composite = CompositeTraceSink([BoomSink(), agg])
        composite.write(_record())
        assert agg.snapshot()["requests"] == 1


class FakeMlflowClient:
    """Records the MLflow client calls the publisher makes."""

    def __init__(self) -> None:
        self.logged: list[tuple[str, str, float, int]] = []  # (run_id, key, value, step)
        self.ended: list[str] = []

    def get_experiment_by_name(self, name: str):
        return None

    def create_experiment(self, name: str) -> str:
        return "exp-1"

    def create_run(self, experiment_id: str, run_name: str):
        class _Info:
            run_id = "run-1"

        class _Run:
            info = _Info()

        assert experiment_id == "exp-1"
        return _Run()

    def log_metric(self, run_id: str, key: str, value: float, step: int = 0) -> None:
        self.logged.append((run_id, key, value, step))

    def set_terminated(self, run_id: str) -> None:
        self.ended.append(run_id)


async def test_app_startup_and_shutdown_never_wait_on_mlflow(tmp_path, monkeypatch) -> None:
    # Observability is best-effort: the publisher connects inside its
    # background task, so a slow/hanging MLflow (black-holed host, long HTTP
    # retries) must delay neither startup nor shutdown. Simulated with a start
    # that blocks far longer than the asserted wall-clock budget.
    import time
    from time import perf_counter

    from litestar.testing import AsyncTestClient

    from litestar_gateway.app import create_app
    from litestar_gateway.config import Settings
    from litestar_gateway.infrastructure.observability.mlflow_metrics import (
        MlflowMetricsPublisher,
    )

    monkeypatch.setattr(MlflowMetricsPublisher, "start", lambda self: time.sleep(10))

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'obs.db'}",
        admin_email="admin@example.com",
        master_key="master-secret",
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
        mlflow_tracking_uri="http://127.0.0.1:1",  # nothing listens here
        mlflow_metrics_interval=60,
    )
    began = perf_counter()
    async with AsyncTestClient(app=create_app(settings)) as client:
        assert (await client.get("/health")).status_code == 200
    assert perf_counter() - began < 5.0  # startup + shutdown, not the 10s sleep


class TestPublisher:
    def test_publishes_snapshot_as_run_metrics(self) -> None:
        agg = MetricsAggregator()
        agg.write(_record())
        client = FakeMlflowClient()
        publisher = MlflowMetricsPublisher(
            agg, experiment_name="gw", run_name="gateway-metrics-test", client=client
        )

        publisher.start()
        publisher.publish_once()

        keys = {key for _, key, _, _ in client.logged}
        assert "requests" in keys
        assert "latency_ms_avg" in keys
        values = {key: value for _, key, value, _ in client.logged}
        assert values["requests"] == 1.0

    def test_steps_increase_per_publish(self) -> None:
        agg = MetricsAggregator()
        client = FakeMlflowClient()
        publisher = MlflowMetricsPublisher(agg, experiment_name="gw", run_name="r", client=client)
        publisher.start()
        publisher.publish_once()
        publisher.publish_once()

        steps = sorted({step for _, _, _, step in client.logged})
        assert steps == [0, 1]

    def test_close_terminates_the_run(self) -> None:
        agg = MetricsAggregator()
        client = FakeMlflowClient()
        publisher = MlflowMetricsPublisher(agg, experiment_name="gw", run_name="r", client=client)
        publisher.start()
        publisher.close()
        assert client.ended == ["run-1"]
