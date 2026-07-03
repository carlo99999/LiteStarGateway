"""No-op trace sink, used when no MLflow tracking URI is configured."""

from __future__ import annotations

from litestar_gateway.domain.entities import TraceRecord


class NullTraceSink:
    def write(self, record: TraceRecord) -> None:
        return None
