"""Publish aggregated gateway metrics to MLflow as time-series run metrics.

MLflow is the gateway's observability backend (`MLFLOW_TRACKING_URI`: the
compose-provided server, any classic server, or Databricks). Traces already
land there per call; this publisher adds the fleet-level view: one long-lived
run per app instance (named after the host) whose metrics — requests, errors,
latency, tokens, cost, dropped traces — are logged every interval and charted
natively by the MLflow UI in the same experiment as the traces.

`publish_once`/`start`/`close` are synchronous (the MLflow client blocks);
the lifespan loop calls them off the event loop, exactly like the trace sink.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
from litestar import Litestar

from litestar_gateway.infrastructure.observability.aggregator import MetricsAggregator

logger = logging.getLogger("litestar_gateway.observability")


class MlflowMetricsPublisher:
    def __init__(
        self,
        aggregator: MetricsAggregator,
        *,
        experiment_name: str,
        run_name: str,
        client: Any | None = None,
        tracking_uri: str | None = None,
    ) -> None:
        self._aggregator = aggregator
        self._experiment_name = experiment_name
        self._run_name = run_name
        self._tracking_uri = tracking_uri
        self._client = client
        self._run_id: str | None = None
        self._step = 0

    def start(self) -> None:
        """Resolve the experiment and open this instance's metrics run."""
        if self._client is None:
            import mlflow
            from mlflow import MlflowClient

            if self._tracking_uri:
                mlflow.set_tracking_uri(self._tracking_uri)
            self._client = MlflowClient()
        existing = self._client.get_experiment_by_name(self._experiment_name)
        experiment_id = (
            existing.experiment_id
            if existing
            else self._client.create_experiment(self._experiment_name)
        )
        run = self._client.create_run(experiment_id, run_name=self._run_name)
        self._run_id = run.info.run_id

    def publish_once(self) -> None:
        if self._client is None or self._run_id is None:  # pragma: no cover - guarded by start()
            return
        # The step advances even if a metric write fails midway: a retried
        # publish must never reuse a step already partially written (it would
        # mix two different snapshots under one point on the charts).
        try:
            for key, value in self._aggregator.snapshot().items():
                self._client.log_metric(self._run_id, key, value, step=self._step)
        finally:
            self._step += 1

    def close(self) -> None:
        if self._client is not None and self._run_id is not None:
            self._client.set_terminated(self._run_id)


def default_run_name() -> str:
    return f"gateway-metrics-{socket.gethostname()}"


def make_metrics_publisher(
    aggregator: MetricsAggregator,
    *,
    tracking_uri: str,
    experiment_name: str,
    interval_seconds: int,
):
    """Litestar lifespan: publish the aggregator to MLflow every interval."""

    publisher = MlflowMetricsPublisher(
        aggregator,
        experiment_name=experiment_name,
        run_name=default_run_name(),
        tracking_uri=tracking_uri,
    )

    started = False

    async def _run() -> None:
        # start() performs network I/O (with the MLflow client's own retries);
        # it runs inside this background task so app startup NEVER waits on
        # MLflow — if it's down, metrics are skipped with a warning.
        nonlocal started
        try:
            # abandon_on_cancel: shutdown must never wait for an in-flight
            # blocking MLflow call (its own HTTP retries can take minutes
            # against a black-holed host); the daemon worker thread is dropped.
            await anyio.to_thread.run_sync(publisher.start, abandon_on_cancel=True)
            started = True
        except Exception:
            logger.warning("MLflow metrics run could not be started", exc_info=True)
            return
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                await anyio.to_thread.run_sync(publisher.publish_once, abandon_on_cancel=True)
            except Exception:  # metrics must never take the app down
                logger.warning("failed to publish metrics to MLflow", exc_info=True)

    @asynccontextmanager
    async def lifespan(_: Litestar) -> AsyncIterator[None]:
        task = asyncio.create_task(_run())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if started:
                with contextlib.suppress(Exception):
                    await anyio.to_thread.run_sync(publisher.close, abandon_on_cancel=True)

    return lifespan
