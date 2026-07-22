"""Regression tests for ISSUE-010: every Playground call is governed."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from litestar_gateway.application.playground_service import PlaygroundService
from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.domain.entities import Model, ModelType, Provider
from litestar_gateway.domain.exceptions import (
    BudgetExceeded,
    InvalidPlaygroundRequest,
    RateLimited,
)
from litestar_gateway.domain.ports import ModelRepository
from litestar_gateway.domain.routing import CandidateModel, QualityTier, RouterConfig

TEAM = uuid4()


def _model(name: str) -> Model:
    return Model(
        id=uuid4(),
        team_id=TEAM,
        name=name,
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gpt-4o",
        params={},
        params_enforced={},
        api_version=None,
        input_cost_per_token=1e-6,
        output_cost_per_token=2e-6,
        enabled=True,
        created_at=datetime.now(UTC),
    )


class _Models:
    def __init__(self, models: dict[str, Model]) -> None:
        self._models = models

    async def get_by_name(self, team_id: UUID | None, name: str) -> Model | None:
        return self._models.get(name)


class _Completion:
    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.calls: list[tuple[UUID, UUID | None, dict[str, Any]]] = []
        self.active = 0
        self.max_active = 0
        self._fail_with = fail_with

    async def chat_completion(
        self, team_id: UUID, api_key_id: UUID | None, request: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((team_id, api_key_id, request))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            if self._fail_with is not None:
                raise self._fail_with
            model = str(request["model"])
            return {
                "choices": [{"message": {"content": f"hi from {model}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        finally:
            self.active -= 1


def _service(
    completion: _Completion,
    *,
    names: tuple[str, ...] = ("a", "b", "c"),
    max_models: int = 5,
    max_concurrency: int = 2,
) -> PlaygroundService:
    return PlaygroundService(
        models=cast("ModelRepository", _Models({name: _model(name) for name in names})),
        completion_service=cast("Any", completion),
        max_models=max_models,
        max_concurrency=max_concurrency,
    )


async def test_compare_deduplicates_aliases_in_request_order() -> None:
    completion = _Completion()

    results = await _service(completion).compare(
        TEAM,
        ["a", "a", "b", "a"],
        [{"role": "user", "content": "hi"}],
    )

    assert [result.model_name for result in results] == ["a", "b"]
    assert [call[2]["model"] for call in completion.calls] == ["a", "b"]


async def test_compare_rejects_more_than_the_configured_model_limit() -> None:
    completion = _Completion()

    with pytest.raises(InvalidPlaygroundRequest, match="at most 2"):
        await _service(completion, max_models=2).compare(
            TEAM,
            ["a", "b", "c"],
            [{"role": "user", "content": "hi"}],
        )

    assert completion.calls == []


async def test_compare_never_exceeds_the_concurrency_limit() -> None:
    completion = _Completion()

    await _service(completion, max_concurrency=2).compare(
        TEAM,
        ["a", "b", "c"],
        [{"role": "user", "content": "hi"}],
    )

    assert completion.max_active == 2


async def test_compare_delegates_to_governed_completion_without_a_fake_api_key() -> None:
    completion = _Completion()

    [result] = await _service(completion).compare(
        TEAM,
        ["a"],
        [{"role": "user", "content": "hi"}],
        max_completion_tokens=32,
    )

    assert result.ok
    assert completion.calls == [
        (
            TEAM,
            None,
            {
                "model": "a",
                "messages": [{"role": "user", "content": "hi"}],
                "max_completion_tokens": 32,
            },
        )
    ]


@pytest.mark.parametrize(
    "error",
    [BudgetExceeded("budget exhausted"), RateLimited("too many requests")],
)
async def test_compare_propagates_governance_rejections(error: Exception) -> None:
    completion = _Completion(fail_with=error)

    with pytest.raises(type(error), match=str(error)):
        await _service(completion, names=("a",)).compare(
            TEAM,
            ["a"],
            [{"role": "user", "content": "hi"}],
        )


@pytest.mark.parametrize("strategy", ["embeddings", "judge", "webhook", "hybrid"])
async def test_router_preview_never_runs_an_external_strategy(strategy: str) -> None:
    class _PreviewService(RouterService):
        async def _run_strategy(self, router: Any, ctx: Any, capable: Any) -> Any:
            raise AssertionError("external preview attempted a side effect")

    candidates = (
        CandidateModel("a", "small", QualityTier.SIMPLE),
        CandidateModel("b", "large", QualityTier.COMPLEX),
    )
    router = RouterConfig(
        id=uuid4(),
        team_id=TEAM,
        name="smart",
        candidates=candidates,
        default_model="b",
        strategy=strategy,
        strategy_config={},
        enabled=True,
        created_at=datetime.now(UTC),
    )
    service = _PreviewService(
        routers=cast("Any", None),
        models=cast("Any", None),
        decisions=cast("Any", None),
    )

    decision = await service.select_preview(
        router,
        {"messages": [{"role": "user", "content": "hello"}]},
        acting_team_id=TEAM,
    )

    assert decision.model_name == "b"
    assert decision.signals == ("preview: external strategy skipped",)
