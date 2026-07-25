"""ResilienceConfig: shared sync/async client kwargs and the pooled async
variant added for Plan 14 Step 3 (registry-shared clients need an explicit,
generously bounded connection pool instead of httpx's low default)."""

from __future__ import annotations

import httpx

from litestar_gateway.infrastructure.llm.resilience import ResilienceConfig


def test_client_kwargs_has_no_http_client() -> None:
    config = ResilienceConfig(timeout=30.0, max_retries=1)
    assert config.client_kwargs == {"timeout": 30.0, "max_retries": 1}


def test_async_client_kwargs_extends_client_kwargs_with_a_pooled_http_client() -> None:
    config = ResilienceConfig(timeout=30.0, max_retries=1)
    kwargs = config.async_client_kwargs

    assert kwargs["timeout"] == 30.0
    assert kwargs["max_retries"] == 1
    assert isinstance(kwargs["http_client"], httpx.AsyncClient)


def test_async_client_kwargs_pool_limits_exceed_httpx_defaults() -> None:
    config = ResilienceConfig()
    client: httpx.AsyncClient = config.async_client_kwargs["http_client"]
    # Only way to inspect the configured Limits post-construction: httpx doesn't
    # expose them back on the client itself, only on the pool it built.
    pool = client._transport._pool  # type: ignore[attr-defined]

    default_limits = httpx.Limits()
    assert pool._max_connections > (default_limits.max_connections or 0)
    assert pool._max_keepalive_connections > (default_limits.max_keepalive_connections or 0)


def test_async_client_kwargs_builds_a_fresh_http_client_each_access() -> None:
    config = ResilienceConfig()
    first = config.async_client_kwargs["http_client"]
    second = config.async_client_kwargs["http_client"]
    assert first is not second  # never shared across two provider client instances
