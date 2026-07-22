"""PlaygroundService: real-call comparison, per-model error isolation, cost calc.

The gateway/repos are faked (no real provider), so these assert the service's
own behavior: it runs each model, computes cost from the model's prices, and a
single model's failure never sinks the batch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from litestar_gateway.application.playground_service import PlaygroundService
from litestar_gateway.domain.entities import Model, ModelType, Provider
from litestar_gateway.domain.ports import CredentialRepository, LLMGateway, ModelRepository

TEAM = uuid4()


def _model(name: str, *, enabled: bool = True, model_type: ModelType = ModelType.CHAT) -> Model:
    return Model(
        id=uuid4(),
        team_id=TEAM,
        name=name,
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=model_type,
        provider_model_id="gpt-4o",
        params={},
        params_enforced={},
        api_version=None,
        input_cost_per_token=1e-06,
        output_cost_per_token=2e-06,
        enabled=enabled,
        created_at=datetime.now(UTC),
    )


class _Models:
    def __init__(self, models: dict[str, Model]) -> None:
        self._models = models

    async def get_by_name(self, team_id: UUID | None, name: str) -> Model | None:
        return self._models.get(name)


class _Credentials:
    async def get_values(self, credential_id: UUID) -> dict[str, str]:
        return {"api_key": "x"}  # pragma: allowlist secret


class _Gateway:
    def __init__(self, fail_for: set[str] | None = None) -> None:
        self._fail_for = fail_for or set()

    async def achat_completion(
        self, request: dict[str, Any], model: Model, credentials: dict[str, str]
    ) -> dict[str, Any]:
        if model.name in self._fail_for:
            raise RuntimeError("upstream boom")
        return {
            "choices": [{"message": {"content": f"hi from {model.name}"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


def _service(models: dict[str, Model], fail_for: set[str] | None = None) -> PlaygroundService:
    return PlaygroundService(
        models=cast("ModelRepository", _Models(models)),
        credentials=cast("CredentialRepository", _Credentials()),
        gateway=cast("LLMGateway", _Gateway(fail_for)),
    )


async def test_compare_returns_ok_with_cost() -> None:
    svc = _service({"a": _model("a")})
    [res] = await svc.compare(TEAM, ["a"], [{"role": "user", "content": "hi"}])
    assert res.ok
    assert res.content == "hi from a"
    assert res.prompt_tokens == 10
    assert res.completion_tokens == 5
    # 10 * 1e-6 + 5 * 2e-6
    assert res.cost == 10 * 1e-06 + 5 * 2e-06
    assert res.latency_ms is not None


async def test_one_failure_does_not_sink_the_batch() -> None:
    svc = _service({"a": _model("a"), "b": _model("b")}, fail_for={"b"})
    results = await svc.compare(TEAM, ["a", "b"], [{"role": "user", "content": "hi"}])
    by_name = {r.model_name: r for r in results}
    assert by_name["a"].ok
    assert not by_name["b"].ok
    assert "boom" in (by_name["b"].error or "")


async def test_unknown_and_wrong_type_models_error() -> None:
    svc = _service({"emb": _model("emb", model_type=ModelType.EMBEDDINGS)})
    results = await svc.compare(TEAM, ["nope", "emb"], [{"role": "user", "content": "hi"}])
    by_name = {r.model_name: r for r in results}
    assert by_name["nope"].error == "unknown model"
    assert not by_name["emb"].ok and "not chat" in (by_name["emb"].error or "")


async def test_router_is_previewed_and_labelled() -> None:
    from litestar_gateway.domain.routing import RouterConfig, RoutingDecision

    router = RouterConfig(
        id=uuid4(),
        team_id=TEAM,
        name="smart",
        candidates=(),
        default_model="a",
        strategy="weighted",
        strategy_config={},
        enabled=True,
        created_at=datetime.now(UTC),
    )

    class _RS:
        async def get_enabled_by_name(self, team_id: UUID, name: str) -> Any:
            return router if name == "smart" else None

        async def select_preview(
            self, r: Any, request: Any, *, acting_team_id: UUID
        ) -> RoutingDecision:
            return RoutingDecision(
                model_name="a",
                strategy="weighted",
                tier=None,
                score=None,
                signals=(),
                decision_ms=1.0,
            )

    svc = PlaygroundService(
        models=cast("ModelRepository", _Models({"a": _model("a")})),
        credentials=cast("CredentialRepository", _Credentials()),
        gateway=cast("LLMGateway", _Gateway()),
        routers=cast("Any", _RS()),
    )
    [res] = await svc.compare(TEAM, ["smart"], [{"role": "user", "content": "hi"}])
    assert res.ok
    assert res.model_name == "smart"
    assert res.chosen_model == "a"
    assert res.content == "hi from a"
