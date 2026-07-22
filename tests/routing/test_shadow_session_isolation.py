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

import pytest

from litestar_gateway.application.routing.service import RouterService, drain_shadow_tasks
from litestar_gateway.domain.entities import Model, ModelType, Provider
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


class RecordingResolver:
    def __init__(self, label: str, calls: list[Call], model: Model | None) -> None:
        self._label = label
        self._calls = calls
        self._model = model

    async def resolve_model(self, team_id: UUID | None, alias: str) -> Model | None:
        self._calls.append((self._label, "resolver", alias))
        return self._model


class JudgeGateway:
    """Always tells the judge to pick 'cheap'."""

    def __init__(self) -> None:
        self.chat_calls = 0
        self.embedding_calls = 0

    async def achat_completion(self, request, model, values) -> dict[str, Any]:
        self.chat_calls += 1
        return {"choices": [{"message": {"content": json.dumps({"choice": "cheap"})}}]}

    async def aembeddings(self, request, model, values) -> dict[str, Any]:
        self.embedding_calls += 1
        return {"data": [{"embedding": [1.0, 0.0]} for _ in request["input"]]}


class FakeDecisionLog:
    async def record(self, decision: RoutingDecisionRecord) -> None:
        return None


class ShadowLog:
    def __init__(self, done: asyncio.Event) -> None:
        self._done = done

    async def record(self, decision: RoutingDecisionRecord) -> None:
        self._done.set()


def _model(name: str, model_type: ModelType = ModelType.CHAT) -> Model:
    return Model(
        id=uuid4(),
        team_id=TEAM_ID,
        name=name,
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=model_type,
        provider_model_id=name,
        params={},
        api_version=None,
        input_cost_per_token=None,
        output_cost_per_token=None,
        enabled=True,
        created_at=datetime.now(UTC),
    )


def _router(shadow_strategy: str = "judge") -> RouterConfig:
    shadow = (
        {"judge_model": "judge-model"}
        if shadow_strategy == "judge"
        else {
            "embedding_model": "embed-model",
            "routes": [
                {
                    "name": "smalltalk",
                    "target_model": "cheap",
                    "utterances": ["ciao"],
                    "threshold": 0.8,
                }
            ],
        }
    )
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
        strategy_config={"shadow": shadow},
        enabled=True,
        created_at=datetime.now(UTC),
        shadow_strategy=shadow_strategy,
    )


async def test_shadow_judge_lookups_use_their_own_repositories() -> None:
    calls: list[Call] = []
    done = asyncio.Event()
    repos_lifecycle: list[str] = []

    @asynccontextmanager
    async def shadow_decisions() -> AsyncIterator[ShadowLog]:
        yield ShadowLog(done)

    judge = _model("judge-model")

    @asynccontextmanager
    async def shadow_repos() -> AsyncIterator[
        tuple[RecordingModels, RecordingCredentials, RecordingResolver]
    ]:
        repos_lifecycle.append("opened")
        try:
            yield (
                RecordingModels("shadow", calls),
                RecordingCredentials("shadow", calls),
                RecordingResolver("shadow", calls, judge),
            )
        finally:
            repos_lifecycle.append("closed")

    service = RouterService(
        routers=SimpleNamespace(),  # type: ignore[arg-type]  # unused by route()
        models=RecordingModels("request", calls),  # type: ignore[arg-type]
        decisions=FakeDecisionLog(),  # type: ignore[arg-type]
        shadow_decisions=shadow_decisions,  # type: ignore[arg-type]
        credentials=RecordingCredentials("request", calls),  # type: ignore[arg-type]
        gateway=JudgeGateway(),  # type: ignore[arg-type]
        shadow_repos=shadow_repos,  # type: ignore[arg-type]
    )
    decision = await service.route(
        _router(),
        {"messages": [{"role": "user", "content": "Ciao, grazie!"}]},
        acting_team_id=TEAM_ID,
    )
    assert decision.model_name == "cheap"  # active complexity → SIMPLE

    await asyncio.wait_for(done.wait(), timeout=2)
    # The judge's model/credential lookups ran on the shadow task's own
    # repositories (own session), never on the request-scoped ones.
    assert ("shadow", "resolver", "judge-model") in calls
    assert not [call for call in calls if call[1] == "models"]
    assert any(label == "shadow" and port == "credentials" for label, port, _ in calls)
    assert not [call for call in calls if call[0] == "request"]
    assert repos_lifecycle == ["opened", "closed"]


@pytest.mark.parametrize(
    ("shadow_strategy", "alias"),
    [("judge", "judge-model"), ("embeddings", "embed-model")],
)
async def test_shadow_tombstone_never_falls_through_to_legacy_lookup_or_provider(
    shadow_strategy: str, alias: str
) -> None:
    """A detached shadow resolver returning None models a revoked/deleted alias.

    The active complexity decision remains safe while the shadow task must not
    bypass the registry through its legacy model repository or provider.
    """
    calls: list[Call] = []
    gateway = JudgeGateway()
    shadow_records: list[RoutingDecisionRecord] = []

    class RecordingLog:
        async def record(self, decision: RoutingDecisionRecord) -> None:
            shadow_records.append(decision)

    @asynccontextmanager
    async def shadow_decisions() -> AsyncIterator[RecordingLog]:
        yield RecordingLog()

    @asynccontextmanager
    async def shadow_repos() -> AsyncIterator[
        tuple[RecordingModels, RecordingCredentials, RecordingResolver]
    ]:
        yield (
            RecordingModels("shadow", calls),
            RecordingCredentials("shadow", calls),
            RecordingResolver("shadow", calls, None),
        )

    service = RouterService(
        routers=SimpleNamespace(),  # type: ignore[arg-type]
        models=RecordingModels("request", calls),  # type: ignore[arg-type]
        decisions=FakeDecisionLog(),  # type: ignore[arg-type]
        shadow_decisions=shadow_decisions,  # type: ignore[arg-type]
        credentials=RecordingCredentials("request", calls),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        shadow_repos=shadow_repos,  # type: ignore[arg-type]
    )

    decision = await service.route(
        _router(shadow_strategy),
        {"messages": [{"role": "user", "content": "Ciao, grazie!"}]},
        acting_team_id=TEAM_ID,
    )
    await drain_shadow_tasks(timeout=2)

    assert decision.model_name == "cheap"
    assert ("shadow", "resolver", alias) in calls
    assert not [call for call in calls if call[1] in {"models", "credentials"}]
    assert gateway.chat_calls == gateway.embedding_calls == 0
    assert shadow_records == []
