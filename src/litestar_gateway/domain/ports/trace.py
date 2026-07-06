"""Port — observability trace sink."""

from __future__ import annotations

from typing import Protocol

from litestar_gateway.domain.entities import TraceRecord


class TraceSink(Protocol):
    """Observability sink. `write` is synchronous — the dispatcher's worker calls
    it off the event loop (MLflow's client is blocking)."""

    def write(self, record: TraceRecord) -> None: ...
