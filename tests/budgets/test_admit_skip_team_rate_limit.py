"""UsageMeter.admit()'s skip_team_rate_limit param (Plan 05 Phase 1).

A cross-provider failover retry must reuse the same team-RPM hit already
taken on attempt #1 -- re-checking it on every retry would silently turn
one logical client request into N rate-limit consumptions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.entities import Model, ModelType, Provider, Team
from litestar_gateway.domain.ports.rate_limiter import RateLimitDecision

TEAM_ID = uuid4()


def _model() -> Model:
    return Model(
        id=uuid4(),
        team_id=TEAM_ID,
        name="m",
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gpt-4o",
        params={},
        params_enforced={},
        api_version=None,
        input_cost_per_token=0.01,
        output_cost_per_token=0.01,
        enabled=True,
        created_at=datetime.now(UTC),
    )


class _FakeUsage:
    async def spend_since(self, team_id, since):
        return 0.0

    async def record(self, event):
        return None

    async def enqueue_pending(self, event):
        return None


class _FakeRateLimiter:
    def __init__(self) -> None:
        self.hits: list[str] = []

    async def hit(self, key: str, limit: int, *, window_seconds: int = 60) -> RateLimitDecision:
        self.hits.append(key)
        return RateLimitDecision(allowed=True, retry_after=0)


class _FakeTeams:
    async def get(self, team_id):
        return Team(
            id=team_id,
            organization_id=uuid4(),
            name="t",
            created_at=datetime.now(UTC),
            rate_limit_rpm=100,
        )


async def test_admit_hits_team_rate_limit_by_default() -> None:
    rate_limiter = _FakeRateLimiter()
    meter = UsageMeter(
        usage=_FakeUsage(),  # type: ignore[arg-type]
        emit_trace=lambda trace: None,
        rate_limiter=rate_limiter,  # type: ignore[arg-type]
        teams=_FakeTeams(),  # type: ignore[arg-type]
    )
    model = _model()

    await meter.admit(TEAM_ID, model, model.merge_params({}))
    await meter.admit(TEAM_ID, model, model.merge_params({}))

    assert rate_limiter.hits == [f"team:{TEAM_ID}", f"team:{TEAM_ID}"]


async def test_admit_skips_team_rate_limit_on_retry() -> None:
    rate_limiter = _FakeRateLimiter()
    meter = UsageMeter(
        usage=_FakeUsage(),  # type: ignore[arg-type]
        emit_trace=lambda trace: None,
        rate_limiter=rate_limiter,  # type: ignore[arg-type]
        teams=_FakeTeams(),  # type: ignore[arg-type]
    )
    model = _model()

    await meter.admit(TEAM_ID, model, model.merge_params({}))
    await meter.admit(TEAM_ID, model, model.merge_params({}), skip_team_rate_limit=True)

    assert rate_limiter.hits == [f"team:{TEAM_ID}"]
