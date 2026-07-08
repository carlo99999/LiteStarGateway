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
import botocore.exceptions
import httpx
import openai

from litestar_gateway.domain.exceptions import (
    DomainError,
    UpstreamAuthFailed,
    UpstreamError,
    UpstreamRateLimited,
    UpstreamRequestRejected,
    UpstreamTimeout,
    UpstreamUnavailable,
)

# Timeouts subclass the connection errors in both SDKs, so they must be checked first.
_TIMEOUT_TYPES = (
    httpx.TimeoutException,
    openai.APITimeoutError,
    anthropic.APITimeoutError,
    botocore.exceptions.ReadTimeoutError,
    botocore.exceptions.ConnectTimeoutError,
)
_CONNECTION_TYPES = (
    httpx.TransportError,
    openai.APIConnectionError,
    anthropic.APIConnectionError,
    botocore.exceptions.ConnectionError,
)

# AWS can report throttling as HTTP 400 with one of these error codes, so the
# code must be checked before the status.
_AWS_THROTTLE_CODES = frozenset(
    {"ThrottlingException", "TooManyRequestsException", "ProvisionedThroughputExceededException"}
)

# Mid-stream Bedrock failures arrive as botocore's EventStreamError, whose
# parsed response carries only Error.Code (no ResponseMetadata, hence no HTTP
# status), so the status must be derived from the code. Statuses mirror what
# the same errors carry on the non-streaming Converse call.
_AWS_ERROR_CODE_STATUS = {
    "InternalServerException": 500,
    "ModelStreamErrorException": 502,
    "ServiceUnavailableException": 503,
    "ValidationException": 400,
    # Model overloaded/not yet provisioned for on-demand throughput - same
    # bucket as ServiceUnavailableException (>=500 -> UpstreamUnavailable).
    "ModelNotReadyException": 503,
    # The model took too long to respond mid-stream - a gateway timeout.
    "ModelTimeoutException": 504,
    # The gateway's own AWS credential lacks bedrock:InvokeModel* on this
    # model/resource - the same ops-incident bucket as 401/403.
    "AccessDeniedException": 403,
}


def _aws_error_code(exc: Exception) -> str | None:
    # botocore's ClientError carries the parsed error response as a dict.
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        return code if isinstance(code, str) else None
    return None


def _status_code(exc: Exception) -> int | None:
    # openai/anthropic status errors expose `status_code`; google-genai's APIError
    # exposes `code`. Both are plain ints.
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    # botocore's ClientError: the status lives in the parsed response dict.
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        value = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if isinstance(value, int):
            return value
        # No ResponseMetadata: an EventStreamError (mid-stream Bedrock failure).
        # Fall back to the AWS error code so it doesn't escape as an opaque 500.
        return _AWS_ERROR_CODE_STATUS.get(_aws_error_code(exc) or "")
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
    if _aws_error_code(exc) in _AWS_THROTTLE_CODES:
        status = 429
    if status == 429:
        return UpstreamRateLimited(
            "upstream provider rate limited the request", retry_after=_retry_after(exc)
        )
    if status is not None and status >= 500:
        return UpstreamUnavailable(f"upstream provider unavailable (status {status})")
    # Remaining 4xx: classify instead of falling through to an opaque 500.
    # 401/403 mean the gateway's own upstream credential is bad (expired or
    # rotated key) - an ops incident, surfaced as 502; other 4xx mean the
    # provider refused this particular request (e.g. an out-of-range param the
    # allowlist passed through) - the client's 400. Status only, no provider
    # body (never echo upstream messages that could carry internals).
    if status in (401, 403):
        return UpstreamAuthFailed(
            f"upstream provider rejected the gateway credential (status {status})"
        )
    if status is not None and 400 <= status < 500:
        return UpstreamRequestRejected(f"upstream provider rejected the request (status {status})")
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
