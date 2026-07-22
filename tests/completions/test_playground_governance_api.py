"""HTTP proof that Playground calls share inference governance and accounting."""

from __future__ import annotations

import pytest
from litestar.status_codes import (
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
    HTTP_402_PAYMENT_REQUIRED,
    HTTP_429_TOO_MANY_REQUESTS,
)
from litestar.testing import AsyncTestClient

from .conftest import _bearer, _patch, _setup_team, _team_usage


def _request(team_id: str, model_names: list[str] | None = None) -> dict:
    return {
        "team_id": team_id,
        "model_names": model_names or ["m"],
        "messages": [{"role": "user", "content": "hello"}],
        "max_completion_tokens": 8,
    }


async def test_playground_records_usage_and_audit(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    _key, team_id, admin = await _setup_team(
        client,
        max_output_tokens=8,
        input_cost_per_token=1e-6,
        output_cost_per_token=2e-6,
    )

    response = await client.post(
        "/playground/compare", json=_request(team_id), headers=_bearer(admin)
    )

    assert response.status_code == HTTP_201_CREATED, response.text
    assert response.json()[0]["ok"] is True
    usage = await _team_usage(client, team_id, admin)
    assert len(usage) == 1
    assert usage[0]["calls"] == 1
    assert usage[0]["cost"] == pytest.approx(3e-6)
    # Session usage contributes to team/budget totals but must not be assigned
    # to the unrelated API key created by the inference harness.
    key_spending = (
        await client.get(f"/teams/{team_id}/keys/spending", headers=_bearer(admin))
    ).json()
    assert len(key_spending) == 1
    assert key_spending[0]["calls"] == 0
    events = (await client.get("/audit", headers=_bearer(admin))).json()
    playground = [event for event in events if event["action"] == "playground.compare"]
    assert len(playground) == 1
    assert playground[0]["target_id"] == team_id
    assert playground[0]["actor_type"] == "user"
    assert playground[0]["actor_email"] == "admin@example.com"
    assert playground[0]["detail"] == "models=1 succeeded=1"


async def test_playground_obeys_team_budget(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    _key, team_id, admin = await _setup_team(
        client,
        max_output_tokens=8,
        input_cost_per_token=1.0,
        output_cost_per_token=1.0,
    )
    budget = await client.put(
        f"/teams/{team_id}/budget",
        json={"limit_cost": 0.01, "window": "monthly"},
        headers=_bearer(admin),
    )
    assert budget.status_code == 200, budget.text

    first = await client.post("/playground/compare", json=_request(team_id), headers=_bearer(admin))
    second = await client.post(
        "/playground/compare", json=_request(team_id), headers=_bearer(admin)
    )

    assert first.status_code == HTTP_201_CREATED, first.text
    assert second.status_code == HTTP_402_PAYMENT_REQUIRED, second.text
    assert (await _team_usage(client, team_id, admin))[0]["calls"] == 1


async def test_playground_obeys_team_rate_limit(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    _key, team_id, admin = await _setup_team(client)
    updated = await client.patch(
        f"/teams/{team_id}",
        json={"name": "Core", "rate_limit_rpm": 1},
        headers=_bearer(admin),
    )
    assert updated.status_code == 200, updated.text

    first = await client.post("/playground/compare", json=_request(team_id), headers=_bearer(admin))
    second = await client.post(
        "/playground/compare", json=_request(team_id), headers=_bearer(admin)
    )

    assert first.status_code == HTTP_201_CREATED, first.text
    assert second.status_code == HTTP_429_TOO_MANY_REQUESTS, second.text


@pytest.mark.parametrize(
    "body",
    [
        {"model_names": ["m"] * 6},
        {"model_names": []},
        {"max_completion_tokens": 0},
        {"messages": []},
    ],
)
async def test_playground_validates_expensive_input_at_the_boundary(
    client: AsyncTestClient, body: dict[str, object]
) -> None:
    _key, team_id, admin = await _setup_team(client)
    request = {**_request(team_id), **body}

    response = await client.post("/playground/compare", json=request, headers=_bearer(admin))

    assert response.status_code == HTTP_400_BAD_REQUEST, response.text
