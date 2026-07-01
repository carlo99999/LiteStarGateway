"""Build the trace sink from settings: MLflow if a tracking URI is set, else no-op."""

from __future__ import annotations

from litestar_test.config import Settings
from litestar_test.domain.ports import TraceSink
from litestar_test.infrastructure.observability.null_sink import NullTraceSink


def build_trace_sink(settings: Settings) -> TraceSink:
    if settings.mlflow_tracking_uri:
        # Imported lazily so mlflow is only needed when tracing is enabled.
        from litestar_test.infrastructure.observability.mlflow_sink import MLflowTraceSink

        return MLflowTraceSink(settings.mlflow_tracking_uri, settings.mlflow_experiment)
    return NullTraceSink()
