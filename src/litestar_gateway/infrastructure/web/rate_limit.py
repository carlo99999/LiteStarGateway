"""Rate limiting: per-IP guardrails for inference and auth endpoints.

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

RateUnit = Literal["second", "minute", "hour", "day"]

# Inference (`/v1/*`): per IP — bounds request rate before auth runs. Per-client
# provider *spend* is bounded separately by the budget gate (admit/InFlightSpend).
INFERENCE_RATE_LIMIT: tuple[RateUnit, int] = ("minute", 120)
# Auth (`/login`, `/signup`): per IP — bounds brute force and account spam.
AUTH_RATE_LIMIT: tuple[RateUnit, int] = ("minute", 20)
# SCIM (`/scim/v2/*`): per IP — the provisioning-token surface authenticates by a
# DB-backed token-hash lookup *before* any other check, so an unauthenticated
# caller flooding garbage tokens forces one DB round-trip per request (M49). More
# generous than the human auth limit (machine IdP provisioning is paced but can
# burst) while still bounding the flood; its own store, so SCIM traffic and login
# don't consume each other's bucket.
SCIM_RATE_LIMIT: tuple[RateUnit, int] = ("minute", 60)


def _inference_identifier(request: Request[Any, Any, Any]) -> str:
    """Key inference calls by client IP.

    The limiter runs *before* authentication, so it must key on something an
    unauthenticated caller cannot cheaply vary. Keying by the bearer token let an
    attacker mint a fresh bucket per random token (M33), escaping the limit and
    flooding the auth-layer DB lookup unthrottled. IP-keying bounds those floods;
    per-client provider spend is bounded separately by the budget gate.
    """
    return f"ip::{get_remote_address(request)}"


def build_inference_rate_limit(
    requests_per_minute: int = INFERENCE_RATE_LIMIT[1],
) -> RateLimitConfig:
    return RateLimitConfig(
        rate_limit=("minute", requests_per_minute),
        identifier_for_request=_inference_identifier,
        store="rate_limit_inference",
    )


def build_auth_rate_limit() -> RateLimitConfig:
    """Per-IP limiter for the public, unauthenticated auth endpoints."""
    return RateLimitConfig(
        rate_limit=AUTH_RATE_LIMIT,
        store="rate_limit_auth",
    )


def build_scim_rate_limit() -> RateLimitConfig:
    """Per-IP limiter for the IdP-facing SCIM surface (provisioning-token auth
    runs a DB lookup before anything else, so it must be throttled like the other
    pre-auth surfaces)."""
    return RateLimitConfig(
        rate_limit=SCIM_RATE_LIMIT,
        store="rate_limit_scim",
    )
