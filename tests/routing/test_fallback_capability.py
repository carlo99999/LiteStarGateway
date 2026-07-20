"""R6-H17: the §4 fallback must stay within the capability-filtered candidates.

When a strategy fails, the fallback may use `default_model` only if it survived
the hard capability filters; otherwise the first capable candidate (in declared
order) is chosen, so the request never reaches a model that would reject it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.domain.ports import ModelRepository, RouterRepository
from litestar_gateway.domain.routing import (
    CandidateModel,
    QualityTier,
    RouterConfig,
    RoutingDecisionRecord,
)


class RecordingDecisionLog:
    """In-memory RoutingDecisionLog capturing persisted records."""

    def __init__(self) -> None:
        self.records: list[RoutingDecisionRecord] = []

    async def record(self, decision: RoutingDecisionRecord) -> None:
        self.records.append(decision)

    async def update_usage(
        self, decision_id: UUID, prompt_tokens: int, completion_tokens: int
    ) -> None:
        return None

    async def list_decisions(
        self,
        team_id: UUID,
        router_id: UUID,
        *,
        strategy: str | None = None,
        chosen_model: str | None = None,
        is_shadow: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RoutingDecisionRecord]:
        return list(self.records)

    async def distribution(
        self, team_id: UUID, router_id: UUID
    ) -> list[tuple[str, str | None, bool, int]]:
        return []

    async def savings(self, team_id: UUID, router_id: UUID) -> tuple[float, int, int]:
        return (0.0, 0, 0)

    async def platform_savings(self) -> tuple[float, int, int]:
        return (0.0, 0, 0)

    async def team_savings(self, team_id: UUID) -> tuple[float, int, int]:
        return (0.0, 0, 0)


def _router(default_model: str) -> RouterConfig:
    return RouterConfig(
        id=uuid4(),
        team_id=uuid4(),
        name="auto",
        candidates=(
            CandidateModel(
                model_name="vision-a",
                description="small vision",
                quality_tier=QualityTier.SIMPLE,
                supports_vision=True,
            ),
            CandidateModel(
                model_name="vision-b",
                description="large vision",
                quality_tier=QualityTier.COMPLEX,
                supports_vision=True,
            ),
            CandidateModel(
                model_name="text-only",
                description="no vision",
                quality_tier=QualityTier.COMPLEX,
            ),
        ),
        default_model=default_model,
        # Unreachable webhook → the strategy raises → §4 fallback path.
        strategy="webhook",
        strategy_config={"url": "http://127.0.0.1:9/route", "timeout_ms": 100},
        enabled=True,
        created_at=datetime.now(UTC),
    )


def _service(log: RecordingDecisionLog) -> RouterService:
    # route() never touches the router/model repositories for this strategy.
    return RouterService(
        routers=cast("RouterRepository", None),
        models=cast("ModelRepository", None),
        decisions=log,
    )


_VISION_REQUEST = {
    "model": "auto",
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is in this picture?"},
                {"type": "image_url", "image_url": {"url": "https://x/img.png"}},
            ],
        }
    ],
}


async def test_fallback_skips_incapable_default_model() -> None:
    """default_model lacks vision → fallback picks the first capable candidate."""
    log = RecordingDecisionLog()
    decision = await _service(log).route(_router(default_model="text-only"), _VISION_REQUEST)

    assert decision.model_name == "vision-a"
    assert "fallback" in decision.signals
    assert any("default_model" in signal for signal in decision.signals)
    assert log.records[0].chosen_model == "vision-a"
    assert log.records[0].fallback_used is True


async def test_fallback_keeps_capable_default_model() -> None:
    """default_model survived the filter → fallback still routes there."""
    log = RecordingDecisionLog()
    decision = await _service(log).route(_router(default_model="vision-b"), _VISION_REQUEST)

    assert decision.model_name == "vision-b"
    assert decision.signals == ("fallback",)
    assert log.records[0].fallback_used is True
