"""Service-principal keys: management/inference scope, the disable kill switch,
team isolation, and the no-self-replication rule (SP admin is JWT-only)."""

from __future__ import annotations

from litestar.status_codes import HTTP_201_CREATED, HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN
from litestar.testing import AsyncTestClient

from .conftest import _admin, _bearer, _model, _sp, _sp_key, _team_and_credential


async def test_sp_key_can_manage_its_team(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, cred = await _team_and_credential(client, admin)
    sp = await _sp(client, admin, team)
    key = await _sp_key(client, admin, team, sp, "management")

    created = await client.post(f"/teams/{team}/models", json=_model(cred), headers=_bearer(key))
    assert created.status_code == HTTP_201_CREATED, created.text


async def test_disabled_sp_key_cannot_manage(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, cred = await _team_and_credential(client, admin)
    sp = await _sp(client, admin, team)
    key = await _sp_key(client, admin, team, sp, "management")

    await client.patch(
        f"/teams/{team}/service-principals/{sp}",
        json={"enabled": False},
        headers=_bearer(admin),
    )
    # A disabled SP is a kill switch: its keys fail at authentication (401),
    # before any per-endpoint authorization is even reached.
    denied = await client.post(f"/teams/{team}/models", json=_model(cred), headers=_bearer(key))
    assert denied.status_code == HTTP_401_UNAUTHORIZED


async def test_disabling_sp_is_a_kill_switch_for_inference_too(client: AsyncTestClient) -> None:
    # Disabling an SP must stop ALL its keys, not just management — an inference
    # key keeps spending otherwise, defeating the emergency kill switch.
    admin = await _admin(client)
    team, cred = await _team_and_credential(client, admin)
    sp = await _sp(client, admin, team)
    key = await _sp_key(client, admin, team, sp, "inference")
    await client.post(f"/teams/{team}/models", json=_model(cred), headers=_bearer(admin))

    ok = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(key),
    )
    assert ok.status_code not in (HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN)

    await client.patch(
        f"/teams/{team}/service-principals/{sp}",
        json={"enabled": False},
        headers=_bearer(admin),
    )
    denied = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(key),
    )
    assert denied.status_code == HTTP_401_UNAUTHORIZED


async def test_sp_is_scoped_to_its_own_team(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team_a, cred = await _team_and_credential(client, admin, "A")
    team_b, _ = await _team_and_credential(client, admin, "B")
    sp_a = await _sp(client, admin, team_a)
    key_a = await _sp_key(client, admin, team_a, sp_a, "management")

    resp = await client.post(f"/teams/{team_b}/models", json=_model(cred), headers=_bearer(key_a))
    assert resp.status_code == HTTP_403_FORBIDDEN


async def test_sp_key_can_be_inference_scope(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, cred = await _team_and_credential(client, admin)
    sp = await _sp(client, admin, team)
    key = await _sp_key(client, admin, team, sp, "inference")
    await client.post(f"/teams/{team}/models", json=_model(cred), headers=_bearer(admin))

    # Inference works; management is denied (scope, not SP).
    infer = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(key),
    )
    assert infer.status_code not in (HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN)
    manage = await client.post(
        f"/teams/{team}/models", json=_model(cred, "m2"), headers=_bearer(key)
    )
    assert manage.status_code == HTTP_403_FORBIDDEN


async def test_management_sp_key_cannot_administer_service_principals(
    client: AsyncTestClient,
) -> None:
    # No self-replication: SP administration (create SP, issue SP keys) is
    # JWT-only, even for a management-scoped key.
    admin = await _admin(client)
    team, _ = await _team_and_credential(client, admin)
    sp = await _sp(client, admin, team)
    key = await _sp_key(client, admin, team, sp, "management")

    create_sp = await client.post(
        f"/teams/{team}/service-principals", json={"name": "evil"}, headers=_bearer(key)
    )
    assert create_sp.status_code == HTTP_401_UNAUTHORIZED
    mint = await client.post(
        f"/teams/{team}/service-principals/{sp}/keys",
        json={"name": "evil"},
        headers=_bearer(key),
    )
    assert mint.status_code == HTTP_401_UNAUTHORIZED
    # Nor a personal key on the same team.
    personal = await client.post(f"/teams/{team}/keys", json={"name": "p"}, headers=_bearer(key))
    assert personal.status_code == HTTP_401_UNAUTHORIZED
