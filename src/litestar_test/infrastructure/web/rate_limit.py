"""Rate limiting: per-API-key for inference, per-IP for auth endpoints.

These are conservative guardrails (not finely tuned) to bound provider cost and
brute-force / account-spam attempts. They use Litestar's in-memory store by
default; back them with a shared store (e.g. Redis) for multi-process deploys.

Note: client IP comes from `get_remote_address`, which does NOT trust
`X-Forwarded-For`. Behind a proxy, set the real client address upstream (e.g.
uvicorn's `--proxy-headers`) for the per-IP limit to be meaningful.
"""

from __future__ import annotations

from typing import Any, Literal

from litestar.connection import Request
from litestar.middleware.rate_limit import RateLimitConfig, get_remote_address

from litestar_test.domain.key_generator import hash_key

RateUnit = Literal["second", "minute", "hour", "day"]

# Inference (`/v1/*`): per API key — bounds runaway provider spend per client.
INFERENCE_RATE_LIMIT: tuple[RateUnit, int] = ("minute", 120)
# Auth (`/login`, `/signup`): per IP — bounds brute force and account spam.
AUTH_RATE_LIMIT: tuple[RateUnit, int] = ("minute", 20)

_BEARER = "Bearer "


def _inference_identifier(request: Request[Any, Any, Any]) -> str:
    """Key inference calls by API key when present, else by client IP.

    Keying by the key's hash (never the plaintext) bounds per-key cost; the IP
    fallback bounds anonymous / invalid-token floods that the auth layer rejects.
    """
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith(_BEARER):
        token = authorization[len(_BEARER) :].strip()
        if token:
            return f"key::{hash_key(token)}"
    return f"ip::{get_remote_address(request)}"


def build_inference_rate_limit() -> RateLimitConfig:
    return RateLimitConfig(
        rate_limit=INFERENCE_RATE_LIMIT,
        identifier_for_request=_inference_identifier,
        store="rate_limit_inference",
    )


def build_auth_rate_limit() -> RateLimitConfig:
    """Per-IP limiter for the public, unauthenticated auth endpoints."""
    return RateLimitConfig(
        rate_limit=AUTH_RATE_LIMIT,
        store="rate_limit_auth",
    )
