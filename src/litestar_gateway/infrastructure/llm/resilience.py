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

# The gateway never follows a redirect to a provider. An egress allowlist bounds
# the host it was told to call, and a 307 from that host names a target nobody
# authorized — so the bound has to survive the response, not just the request.
_FOLLOW_REDIRECTS = False


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

    def build_async_http_client(self) -> httpx.AsyncClient:
        """A fresh, generously bounded `httpx.AsyncClient` for an async
        provider client instance meant to be reused (leased from the
        registry) rather than built fresh per call. Built fresh on every
        call, matching the "build once per registry miss" cost model — never
        share the returned object across two provider client instances."""
        return httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=self.max_connections,
                max_keepalive_connections=self.max_keepalive_connections,
            ),
            timeout=self.timeout,
            follow_redirects=_FOLLOW_REDIRECTS,
        )

    def build_sync_http_client(self) -> httpx.Client:
        """The sync twin of `build_async_http_client`.

        It exists for one reason: the SDK's own default client sets
        `follow_redirects=True`, so a sync constructor left to itself is the
        only client the gateway builds that would chase a redirect.
        """
        return httpx.Client(
            limits=httpx.Limits(
                max_connections=self.max_connections,
                max_keepalive_connections=self.max_keepalive_connections,
            ),
            timeout=self.timeout,
            follow_redirects=_FOLLOW_REDIRECTS,
        )

    @property
    def sync_client_kwargs(self) -> dict[str, Any]:
        """`client_kwargs` plus a sync client that does not follow redirects."""
        return {**self.client_kwargs, "http_client": self.build_sync_http_client()}

    @property
    def async_client_kwargs(self) -> dict[str, Any]:
        """`client_kwargs` plus the pooled async client, for OpenAI/Anthropic-
        style constructors that accept `http_client` directly."""
        return {**self.client_kwargs, "http_client": self.build_async_http_client()}

    @property
    def timeout_ms(self) -> int:
        """Timeout in milliseconds, as the google-genai `HttpOptions` expects."""
        return int(self.timeout * 1000)
