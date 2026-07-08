"""R6-C3: shadow strategy DB lookups must not reuse the request-scoped session.

The shadow task is fire-and-forget: it races the request coroutine, which is
still issuing statements on the request-scoped AsyncSession (not safe for
concurrent cross-task use). Judge/embeddings shadow strategies must get their
model/credential repositories from `shadow_repos` — a factory that opens its
own session per run, mirroring `shadow_decisions`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.domain.entities import ModelType
from litestar_gateway.domain.routing import (
    CandidateModel,
    QualityTier,
    RouterConfig,
    RoutingDecisionRecord,
)

TEAM_ID = uuid4()

Call = tuple[str, str, str]  # (repo label, port, argument)


class RecordingModels:
    """ModelRepository fake recording which repository served each lookup."""

    def __init__(self, label: str, calls: list[Call]) -> None:
        self._label = label
        self._calls = calls

    async def get_by_name(self, team_id: UUID, name: str):
        self._calls.append((self._label, "models", name))
        return SimpleNamespace(enabled=True, type=ModelType.CHAT, credential_id=uuid4())


class RecordingCredentials:
    """CredentialRepository fake recording which repository served each lookup."""

    def __init__(self, label: str, calls: list[Call]) -> None:
        self._label = label
        self._calls = calls

    async def get_values(self, credential_id: UUID) -> dict[str, str] | None:
        self._calls.append((self._label, "credentials", str(credential_id)))
        return {"api_key": "k"}  # pragma: allowlist secret


class JudgeGateway:
    """Always tells the judge to pick 'cheap'."""

    async def achat_completion(self, request, model, values) -> dict[str, Any]:
        return {"choices": [{"message": {"content": json.dumps({"choice": "cheap"})}}]}


class FakeDecisionLog:
    async def record(self, decision: RoutingDecisionRecord) -> None:
        return None


class ShadowLog:
    def __init__(self, done: asyncio.Event) -> None:
        self._done = done

    async def record(self, decision: RoutingDecisionRecord) -> None:
        self._done.set()


def _router() -> RouterConfig:
    return RouterConfig(
        id=uuid4(),
        team_id=TEAM_ID,
        name="auto",
        candidates=(
            CandidateModel(
                model_name="cheap", description="small", quality_tier=QualityTier.SIMPLE
            ),
            CandidateModel(model_name="big", description="large", quality_tier=QualityTier.COMPLEX),
        ),
        default_model="big",
        strategy="complexity",
        strategy_config={"shadow": {"judge_model": "judge-model"}},
        enabled=True,
        created_at=datetime.now(UTC),
        shadow_strategy="judge",
    )


async def test_shadow_judge_lookups_use_their_own_repositories() -> None:
    calls: list[Call] = []
    done = asyncio.Event()
    repos_lifecycle: list[str] = []

    @asynccontextmanager
    async def shadow_decisions() -> AsyncIterator[ShadowLog]:
        yield ShadowLog(done)

    @asynccontextmanager
    async def shadow_repos() -> AsyncIterator[tuple[RecordingModels, RecordingCredentials]]:
        repos_lifecycle.append("opened")
        try:
            yield (RecordingModels("shadow", calls), RecordingCredentials("shadow", calls))
        finally:
            repos_lifecycle.append("closed")

    service = RouterService(
        routers=SimpleNamespace(),  # type: ignore[arg-type]  # unused by route()
        models=RecordingModels("request", calls),  # type: ignore[arg-type]
        decisions=FakeDecisionLog(),  # type: ignore[arg-type]
        shadow_decisions=shadow_decisions,
        credentials=RecordingCredentials("request", calls),  # type: ignore[arg-type]
        gateway=JudgeGateway(),  # type: ignore[arg-type]
        shadow_repos=shadow_repos,
    )
    decision = await service.route(
        _router(), {"messages": [{"role": "user", "content": "Ciao, grazie!"}]}
    )
    assert decision.model_name == "cheap"  # active complexity → SIMPLE

    await asyncio.wait_for(done.wait(), timeout=2)
    # The judge's model/credential lookups ran on the shadow task's own
    # repositories (own session), never on the request-scoped ones.
    assert ("shadow", "models", "judge-model") in calls
    assert any(label == "shadow" and port == "credentials" for label, port, _ in calls)
    assert not [call for call in calls if call[0] == "request"]
    assert repos_lifecycle == ["opened", "closed"]
