"""Service-principal administration: validation, CRUD, audit attribution, RBAC."""

from __future__ import annotations

from litestar.status_codes import HTTP_200_OK, HTTP_400_BAD_REQUEST, HTTP_403_FORBIDDEN
from litestar.testing import AsyncTestClient

from .conftest import DEV_PASSWORD, _admin, _bearer, _model, _sp, _sp_key, _team_and_credential


async def test_sp_name_is_validated(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, _ = await _team_and_credential(client, admin)
    for bad in ("", "   ", "x" * 201):
        resp = await client.post(
            f"/teams/{team}/service-principals", json={"name": bad}, headers=_bearer(admin)
        )
        assert resp.status_code == HTTP_400_BAD_REQUEST, bad


async def test_sp_audit_attributes_the_principal_identity(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, cred = await _team_and_credential(client, admin)
    sp = await _sp(client, admin, team, "deployer")
    key = await _sp_key(client, admin, team, sp, "management")
    await client.post(f"/teams/{team}/models", json=_model(cred), headers=_bearer(key))

    audit = (await client.get("/audit", headers=_bearer(admin))).json()
    entry = next(e for e in audit if e["action"] == "model.create")
    assert entry["actor_type"] == "service_principal"
    assert entry["actor_email"] == "sp:deployer"


async def test_sp_crud_and_listing(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, _ = await _team_and_credential(client, admin)
    sp = await _sp(client, admin, team, "one")

    listed = await client.get(f"/teams/{team}/service-principals", headers=_bearer(admin))
    assert listed.status_code == HTTP_200_OK
    assert [s["name"] for s in listed.json()] == ["one"]
    assert listed.json()[0]["enabled"] is True

    patched = await client.patch(
        f"/teams/{team}/service-principals/{sp}",
        json={"enabled": False},
        headers=_bearer(admin),
    )
    assert patched.status_code == HTTP_200_OK
    assert patched.json()["enabled"] is False


async def test_sp_creation_requires_team_management(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team, _ = await _team_and_credential(client, admin)
    # A plain member (not team admin) cannot create SPs.
    invite = (await client.post("/invites", headers=_bearer(admin))).json()["token"]
    await client.post(
        "/signup",
        json={"invite_token": invite, "email": "m@example.com", "password": DEV_PASSWORD},
    )
    await client.post(
        f"/teams/{team}/members",
        json={"email": "m@example.com", "role": "member"},
        headers=_bearer(admin),
    )
    member = (
        await client.post("/login", json={"email": "m@example.com", "password": DEV_PASSWORD})
    ).json()["access_token"]
    resp = await client.post(
        f"/teams/{team}/service-principals", json={"name": "x"}, headers=_bearer(member)
    )
    assert resp.status_code == HTTP_403_FORBIDDEN
