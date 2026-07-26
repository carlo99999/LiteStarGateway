"""Cross-provider failover eligibility (Plan 05, Phase 0)."""

from __future__ import annotations

import pytest

from litestar_gateway.domain.exceptions import (
    BudgetExceeded,
    DomainError,
    ModelNotFound,
    UpstreamAuthFailed,
    UpstreamRateLimited,
    UpstreamRequestRejected,
    UpstreamResponseInvalid,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from litestar_gateway.domain.failover import is_failover_eligible


@pytest.mark.parametrize(
    "exc",
    [
        UpstreamTimeout("timed out"),
        UpstreamRateLimited("rate limited"),
        UpstreamRateLimited("rate limited", retry_after="30"),
        UpstreamUnavailable("5xx"),
        UpstreamResponseInvalid("malformed", {"usage": {}}),
        UpstreamAuthFailed("bad credential"),
    ],
)
def test_transient_upstream_errors_are_eligible(exc: DomainError) -> None:
    assert is_failover_eligible(exc) is True


def test_upstream_request_rejected_is_never_eligible() -> None:
    # The provider validated and refused the request itself; another
    # candidate would fail identically -- surface immediately, never retry.
    assert is_failover_eligible(UpstreamRequestRejected("bad parameter")) is False


@pytest.mark.parametrize(
    "exc",
    [
        BudgetExceeded("over budget"),
        ModelNotFound("no such model"),
        DomainError("generic"),
    ],
)
def test_non_upstream_domain_errors_are_never_eligible(exc: DomainError) -> None:
    assert is_failover_eligible(exc) is False
