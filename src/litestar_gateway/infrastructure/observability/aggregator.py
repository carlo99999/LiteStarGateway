"""In-process metrics aggregation from TraceRecords (a `TraceSink`).

Counts what an operator needs at fleet level — requests, errors (total and per
provider), latency, tokens, cost, dropped traces — so the MLflow publisher can
log them as time-series run metrics. Written by the trace dispatcher's worker
and read by the publisher loop; a lock keeps the two consistent (both touches
are microseconds, nothing blocks on the request path).
"""

from __future__ import annotations

import threading
from collections import Counter

from litestar_gateway.domain.entities import TraceRecord


class MetricsAggregator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()
        # Latency is an interval statistic (avg/max since the last snapshot);
        # counters are cumulative for the process lifetime.
        self._latency_sum = 0.0
        self._latency_count = 0
        self._latency_max = 0.0

    # -- TraceSink protocol ---------------------------------------------------

    def write(self, record: TraceRecord) -> None:
        with self._lock:
            self._counters["requests"] += 1
            self._counters[f"requests.{record.provider}"] += 1
            if record.status == "error":
                self._counters["errors"] += 1
                self._counters[f"errors.{record.provider}"] += 1
            self._counters["prompt_tokens"] += record.prompt_tokens
            self._counters["completion_tokens"] += record.completion_tokens
            # Counter only handles ints; keep cost in micro-USD internally.
            self._counters["_cost_micro_usd"] += round(record.cost * 1_000_000)
            self._latency_sum += record.latency_ms
            self._latency_count += 1
            self._latency_max = max(self._latency_max, record.latency_ms)

    def record_dropped_trace(self) -> None:
        with self._lock:
            self._counters["dropped_traces"] += 1

    def snapshot(self) -> dict[str, float]:
        """Cumulative totals plus interval latency stats (reset on read)."""
        with self._lock:
            snap: dict[str, float] = {
                key: float(value)
                for key, value in self._counters.items()
                if not key.startswith("_")
            }
            snap.setdefault("requests", 0.0)
            snap.setdefault("errors", 0.0)
            snap.setdefault("dropped_traces", 0.0)
            snap.setdefault("prompt_tokens", 0.0)
            snap.setdefault("completion_tokens", 0.0)
            snap["cost_usd"] = self._counters["_cost_micro_usd"] / 1_000_000
            snap["latency_ms_avg"] = (
                self._latency_sum / self._latency_count if self._latency_count else 0.0
            )
            snap["latency_ms_max"] = self._latency_max
            self._latency_sum = 0.0
            self._latency_count = 0
            self._latency_max = 0.0
            return snap
