"""Translate provider SDK exceptions into domain errors at the gateway boundary.

Provider SDK exceptions (`openai.RateLimitError`, `anthropic.APIStatusError`,
`google.genai` errors, raw `httpx` timeouts) are not `DomainError`s, so without
translation they surface as opaque 500s — a real upstream 429 becomes
indistinguishable from a gateway bug and breaks client-side retry/backoff.
The gateway wraps every adapter dispatch with these helpers so upstream
failures map to `UpstreamRateLimited` (429), `UpstreamUnavailable` (502) and
`UpstreamTimeout` (504). Anything unrecognized re-raises unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import anthropic
import httpx
import openai

from litestar_test.domain.exceptions import (
    DomainError,
    UpstreamError,
    UpstreamRateLimited,
    UpstreamTimeout,
    UpstreamUnavailable,
)

# Timeouts subclass the connection errors in both SDKs, so they must be checked first.
_TIMEOUT_TYPES = (httpx.TimeoutException, openai.APITimeoutError, anthropic.APITimeoutError)
_CONNECTION_TYPES = (httpx.TransportError, openai.APIConnectionError, anthropic.APIConnectionError)


def _status_code(exc: Exception) -> int | None:
    # openai/anthropic status errors expose `status_code`; google-genai's APIError
    # exposes `code`. Both are plain ints.
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _retry_after(exc: Exception) -> str | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    value = headers.get("retry-after")
    return value if isinstance(value, str) else None


def translate_upstream_error(exc: Exception) -> UpstreamError | None:
    """Map a provider SDK exception to a domain error, or None if unrecognized."""
    if isinstance(exc, DomainError):
        return None
    if isinstance(exc, _TIMEOUT_TYPES):
        return UpstreamTimeout("upstream provider timed out")
    status = _status_code(exc)
    if status == 429:
        return UpstreamRateLimited(
            "upstream provider rate limited the request", retry_after=_retry_after(exc)
        )
    if status is not None and status >= 500:
        return UpstreamUnavailable(f"upstream provider unavailable (status {status})")
    if isinstance(exc, _CONNECTION_TYPES):
        return UpstreamUnavailable("could not reach upstream provider")
    return None


def _raise_translated(exc: Exception) -> None:
    mapped = translate_upstream_error(exc)
    if mapped is not None:
        raise mapped from exc
    raise exc


def run_translated[T](call: Callable[[], T]) -> T:
    try:
        return call()
    except Exception as exc:
        _raise_translated(exc)
        raise  # unreachable; keeps the type checker happy


async def arun_translated[T](awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except Exception as exc:
        _raise_translated(exc)
        raise


async def translate_stream(
    stream: AsyncIterator[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    """Relay chunks unchanged, translating errors raised mid-stream too (the
    provider can 429/5xx after the first chunk)."""
    try:
        async for item in stream:
            yield item
    except Exception as exc:
        _raise_translated(exc)
        raise
