"""Resilience config applied to provider SDK clients.

A gateway sits in the critical path to external providers it does not control, so
a slow or failing upstream must fail *fast* and *bounded* rather than hanging (the
OpenAI/Anthropic SDK default timeout is ~10 minutes). The OpenAI and Anthropic
clients honour `timeout` + `max_retries` natively (exponential backoff, Retry-After,
correct streaming handling), so we configure them rather than hand-rolling a retry
loop; the Vertex/genai client takes a timeout via `HttpOptions` (milliseconds).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class ResilienceConfig:
    timeout: float = 60.0
    max_retries: int = 2
    # A registry-leased async client is now shared across every concurrent
    # request for one credential, instead of one short-lived pool per single
    # request. httpx's own default (100 max, 20 keepalive) starts queuing
    # requests well before that concurrency is unusual for one credential —
    # measured as a real streaming throughput regression in Plan 14 Step 3.
    # These are a generous, bounded interim default; Step 6 sizes the real
    # per-worker/deployment budget against provider and Postgres capacity.
    max_connections: int = 1000
    max_keepalive_connections: int = 100

    @property
    def client_kwargs(self) -> dict[str, Any]:
        """Kwargs accepted by the OpenAI and Anthropic client constructors."""
        return {"timeout": self.timeout, "max_retries": self.max_retries}

    @property
    def async_client_kwargs(self) -> dict[str, Any]:
        """`client_kwargs` plus an explicit, generously bounded httpx
        connection pool for an async client instance meant to be reused
        (leased from the registry) rather than built fresh per call. A fresh
        `httpx.AsyncClient` is constructed on every access, matching the
        "build once per registry miss" cost model — never share this
        `httpx.AsyncClient` object across two provider client instances."""
        return {
            **self.client_kwargs,
            "http_client": httpx.AsyncClient(
                limits=httpx.Limits(
                    max_connections=self.max_connections,
                    max_keepalive_connections=self.max_keepalive_connections,
                ),
                timeout=self.timeout,
            ),
        }

    @property
    def timeout_ms(self) -> int:
        """Timeout in milliseconds, as the google-genai `HttpOptions` expects."""
        return int(self.timeout * 1000)
