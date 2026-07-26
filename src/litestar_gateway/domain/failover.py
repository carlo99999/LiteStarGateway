"""Cross-provider failover eligibility (Plan 05, Phase 0).

Decides whether a failed dispatch attempt may retry against the next
candidate model in the routing chain, or must surface to the client
immediately. Pure classification over the error type only -- the failover
loop itself (admit/dispatch/settle per attempt, budget single-charge
guarantee) lives in `application/completion_service.py`.
"""

from __future__ import annotations

from litestar_gateway.domain.exceptions import (
    DomainError,
    UpstreamAuthFailed,
    UpstreamRateLimited,
    UpstreamTimeout,
    UpstreamUnavailable,
)

# UpstreamResponseInvalid subclasses UpstreamUnavailable and is eligible too:
# a malformed payload from one provider says nothing about another candidate.
_ELIGIBLE_UPSTREAM_ERRORS = (
    UpstreamTimeout,
    UpstreamRateLimited,
    UpstreamUnavailable,
    UpstreamAuthFailed,
)


def is_failover_eligible(exc: DomainError) -> bool:
    """Whether a failed attempt may retry against the next candidate.

    Eligible: transient, provider-side failures -- timeout, rate limit,
    unavailable/5xx, or an upstream credential problem (an ops issue, not
    the client's fault).

    Terminal, always: `UpstreamRequestRejected` (the provider validated and
    refused the request itself; another candidate would fail identically)
    and every other `DomainError` -- client-facing validation, budget, and
    policy errors have nothing to do with which provider served the call.
    """
    return isinstance(exc, _ELIGIBLE_UPSTREAM_ERRORS)
