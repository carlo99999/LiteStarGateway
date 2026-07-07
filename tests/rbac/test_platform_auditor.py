"""Platform auditor: grant/revoke, read-only cross-team access, RBAC on the grant itself."""

from __future__ import annotations

from conftest import _admin, _bearer, _credential, _login, _model_payload, _signup, _team
from litestar.status_codes import HTTP_200_OK, HTTP_403_FORBIDDEN
from litestar.testing import AsyncTestClient


async def test_admin_grants_and_revokes_auditor_with_audit_trail(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    user_id = await _signup(client, admin, "aud@corp.com")

    granted = await client.patch(
        f"/users/{user_id}/auditor", json={"is_auditor": True}, headers=_bearer(admin)
    )
    assert granted.status_code == HTTP_200_OK, granted.text
    assert granted.json()["is_auditor"] is True

    events = (await client.get("/audit", headers=_bearer(admin))).json()
    assert "user.grant_auditor" in {e["action"] for e in events}


async def test_auditor_reads_audit_and_any_team_usage_but_mutates_nothing(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    cred = await _credential(client, admin)
    user_id = await _signup(client, admin, "aud@corp.com")
    await client.patch(
        f"/users/{user_id}/auditor", json={"is_auditor": True}, headers=_bearer(admin)
    )
    token = await _login(client, "aud@corp.com", "Sup3r-Secret!")

    # Read-only, cross-team (the auditor is NOT a member of the team).
    assert (await client.get("/audit", headers=_bearer(token))).status_code == HTTP_200_OK
    assert (
        await client.get(f"/teams/{team}/usage", headers=_bearer(token))
    ).status_code == HTTP_200_OK
    assert (
        await client.get(f"/teams/{team}/keys/spending", headers=_bearer(token))
    ).status_code == HTTP_200_OK

    for request in (
        client.post(f"/teams/{team}/models", json=_model_payload(cred), headers=_bearer(token)),
        client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(token)),
        client.post("/organizations", json={"name": "Nope"}, headers=_bearer(token)),
    ):
        assert (await request).status_code == HTTP_403_FORBIDDEN


async def test_non_admin_cannot_grant_auditor_and_plain_user_cannot_read_audit(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    user_id = await _signup(client, admin, "plain@corp.com")
    token = await _login(client, "plain@corp.com", "Sup3r-Secret!")

    assert (await client.get("/audit", headers=_bearer(token))).status_code == HTTP_403_FORBIDDEN
    denied = await client.patch(
        f"/users/{user_id}/auditor", json={"is_auditor": True}, headers=_bearer(token)
    )
    assert denied.status_code == HTTP_403_FORBIDDEN


async def test_revoking_auditor_closes_audit_access(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    user_id = await _signup(client, admin, "aud@corp.com")
    await client.patch(
        f"/users/{user_id}/auditor", json={"is_auditor": True}, headers=_bearer(admin)
    )
    token = await _login(client, "aud@corp.com", "Sup3r-Secret!")
    assert (await client.get("/audit", headers=_bearer(token))).status_code == HTTP_200_OK

    revoked = await client.patch(
        f"/users/{user_id}/auditor", json={"is_auditor": False}, headers=_bearer(admin)
    )
    assert revoked.status_code == HTTP_200_OK
    assert revoked.json()["is_auditor"] is False
    assert (await client.get("/audit", headers=_bearer(token))).status_code == HTTP_403_FORBIDDEN
