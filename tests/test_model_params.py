"""Unit tests for Model.merge_params — the three-way param precedence (H15).

`params` are admin defaults the client may override; the sanitized client
request wins over them; `params_enforced` is admin policy applied last and
cannot be overridden by the client.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from litestar_gateway.domain.entities import Model, ModelType, Provider


def _model(
    params: dict[str, Any] | None = None,
    params_enforced: dict[str, Any] | None = None,
) -> Model:
    return Model(
        id=uuid4(),
        team_id=uuid4(),
        name="m",
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gpt-4o",
        params=params or {},
        api_version=None,
        input_cost_per_token=None,
        output_cost_per_token=None,
        enabled=True,
        created_at=datetime.now(UTC),
        params_enforced=params_enforced or {},
    )


def test_client_overrides_default_params() -> None:
    model = _model(params={"temperature": 0.2})
    assert model.merge_params({"temperature": 0.9})["temperature"] == 0.9


def test_default_used_when_client_omits() -> None:
    model = _model(params={"temperature": 0.2})
    assert model.merge_params({})["temperature"] == 0.2


def test_enforced_params_win_over_client() -> None:
    model = _model(params_enforced={"response_format": {"type": "json_object"}})
    merged = model.merge_params({"response_format": {"type": "text"}})
    assert merged["response_format"] == {"type": "json_object"}


def test_enforced_beats_both_default_and_client() -> None:
    model = _model(params={"temperature": 0.2}, params_enforced={"temperature": 0.0})
    assert model.merge_params({"temperature": 0.9})["temperature"] == 0.0


def test_merge_does_not_mutate_request_or_model() -> None:
    request = {"temperature": 0.9}
    model = _model(params={"top_p": 0.5}, params_enforced={"seed": 1})
    model.merge_params(request)
    assert request == {"temperature": 0.9}
    assert model.params == {"top_p": 0.5}
    assert model.params_enforced == {"seed": 1}
