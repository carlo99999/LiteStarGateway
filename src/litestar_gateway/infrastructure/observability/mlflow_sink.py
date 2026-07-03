"""MLflow trace sink — works with classic MLflow and Databricks-managed MLflow.

Only the tracking URI differs (``http://host:5000`` for a classic server,
``databricks`` / ``databricks://profile`` for Databricks — on Databricks the
experiment name must be a workspace path, e.g. ``/Shared/litestar-gateway``, and
auth comes from ``DATABRICKS_HOST`` / ``DATABRICKS_TOKEN``).

Traces are written with an explicit ``experiment_id`` (verified against MLflow
3.x), so no global active-experiment state is touched — safe when routing to a
general and, later, per-team experiments. `write` is synchronous; the dispatcher
calls it off the event loop, and MLflow's own async exporter batches the network
I/O.
"""

from __future__ import annotations

import mlflow
from mlflow import MlflowClient

from litestar_gateway.domain.entities import TraceRecord


class MLflowTraceSink:
    def __init__(self, tracking_uri: str, experiment_name: str) -> None:
        # The async exporter uses the *global* tracking URI, so set it here.
        mlflow.set_tracking_uri(tracking_uri)
        self._client = MlflowClient()
        self._experiment_name = experiment_name
        self._experiment_id: str | None = None

    def _experiment(self) -> str:
        if self._experiment_id is None:
            existing = self._client.get_experiment_by_name(self._experiment_name)
            self._experiment_id = (
                existing.experiment_id
                if existing
                else self._client.create_experiment(self._experiment_name)
            )
        return self._experiment_id

    def write(self, record: TraceRecord) -> None:
        attributes = {
            "team_id": str(record.team_id),
            "api_key_id": str(record.api_key_id),
            "model": record.model_name,
            "provider": record.provider,
            "prompt_tokens": str(record.prompt_tokens),
            "completion_tokens": str(record.completion_tokens),
            "cost": str(record.cost),
            "latency_ms": str(record.latency_ms),
        }
        if record.error_type:
            attributes["error_type"] = record.error_type
        span = self._client.start_trace(
            name=record.operation,
            attributes=attributes,
            tags={"operation": record.operation, "status": record.status},
            experiment_id=self._experiment(),
        )
        self._client.end_trace(span.trace_id, status="OK" if record.status == "ok" else "ERROR")
