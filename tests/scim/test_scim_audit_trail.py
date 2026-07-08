"""SCIM mutations are audited under the provisioning token's identity."""

from __future__ import annotations

from litestar.testing import AsyncTestClient

from .conftest import _admin_headers, _create_user


async def test_scim_actions_are_audited(client: AsyncTestClient) -> None:
    admin = await _admin_headers(client)
    token = (await client.post("/scim-tokens", json={"name": "okta"}, headers=admin)).json()
    scim = {"Authorization": f"Bearer {token['token']}"}
    created = await _create_user(client, scim, "gina@corp.com")
    await client.delete(f"/scim/v2/Users/{created['id']}", headers=scim)

    events = (await client.get("/audit", headers=admin)).json()
    actions = {e["action"] for e in events}
    assert "scim_token.create" in actions
    assert "scim.user.create" in actions
    assert "scim.user.deactivate" in actions
    scim_created = next(e for e in events if e["action"] == "scim.user.create")
    assert scim_created["actor_type"] == "scim_token"
    assert scim_created["actor_email"] == "scim:okta"


async def test_scim_reactivation_is_audited_distinctly(client: AsyncTestClient) -> None:
    admin = await _admin_headers(client)
    token = (await client.post("/scim-tokens", json={"name": "okta"}, headers=admin)).json()
    scim = {"Authorization": f"Bearer {token['token']}"}
    created = await _create_user(client, scim, "hana@corp.com")

    # SCIM disabled the account itself, so SCIM may freely re-enable it.
    await client.delete(f"/scim/v2/Users/{created['id']}", headers=scim)
    reactivated = await client.patch(
        f"/scim/v2/Users/{created['id']}",
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "active", "value": True}],
        },
        headers=scim,
    )
    assert reactivated.status_code == 200, reactivated.text
    assert reactivated.json()["active"] is True

    events = (await client.get("/audit", headers=admin)).json()
    actions = {e["action"] for e in events}
    assert "scim.user.reactivate" in actions
