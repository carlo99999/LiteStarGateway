"""R6-C2: the decisions export dumps raw prompts, so it must require
`decisions:read` (admin/model-manager) — never the billing-oriented
`usage:read` held by billing viewers and the platform auditor bypass."""

from __future__ import annotations

from litestar.status_codes import HTTP_200_OK, HTTP_201_CREATED, HTTP_403_FORBIDDEN
from litestar.testing import AsyncTestClient

from .conftest import (
    _admin,
    _bearer,
    _credential,
    _login,
    _member_token,
    _model_payload,
    _signup,
    _team,
)


async def _router(client: AsyncTestClient, admin: str, team: str) -> str:
    """Create a model + router in the team; returns the router id."""
    cred = await _credential(client, admin)
    model = await client.post(
        f"/teams/{team}/models", json=_model_payload(cred), headers=_bearer(admin)
    )
    assert model.status_code == HTTP_201_CREATED, model.text
    router = await client.post(
        f"/teams/{team}/routers",
        json={
            "name": "auto",
            "default_model": "fast-chat",
            "candidates": [
                {"model_name": "fast-chat", "description": "fast", "quality_tier": "SIMPLE"}
            ],
        },
        headers=_bearer(admin),
    )
    assert router.status_code == HTTP_201_CREATED, router.text
    return router.json()["id"]


async def test_billing_viewer_cannot_export_decisions(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    router = await _router(client, admin, team)
    token = await _member_token(client, admin, team, "bv@corp.com", "billing-viewer")

    # usage:read still works for billing…
    usage = await client.get(f"/teams/{team}/usage", headers=_bearer(token))
    assert usage.status_code == HTTP_200_OK
    # …but the raw-prompt export is out of reach.
    export = await client.get(
        f"/teams/{team}/routers/{router}/decisions/export", headers=_bearer(token)
    )
    assert export.status_code == HTTP_403_FORBIDDEN


async def test_platform_auditor_cannot_export_decisions(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    router = await _router(client, admin, team)
    user_id = await _signup(client, admin, "aud@corp.com")
    await client.patch(
        f"/users/{user_id}/auditor", json={"is_auditor": True}, headers=_bearer(admin)
    )
    token = await _login(client, "aud@corp.com", "Sup3r-Secret!")

    # The auditor bypass keeps its read-only billing visibility…
    usage = await client.get(f"/teams/{team}/usage", headers=_bearer(token))
    assert usage.status_code == HTTP_200_OK
    # …but never raw prompt content in a team it is not a member of.
    export = await client.get(
        f"/teams/{team}/routers/{router}/decisions/export", headers=_bearer(token)
    )
    assert export.status_code == HTTP_403_FORBIDDEN


async def test_team_admin_and_model_manager_still_export_decisions(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    router = await _router(client, admin, team)

    for email, role in (("lead@corp.com", "admin"), ("mm@corp.com", "model-manager")):
        token = await _member_token(client, admin, team, email, role)
        export = await client.get(
            f"/teams/{team}/routers/{router}/decisions/export", headers=_bearer(token)
        )
        assert export.status_code == HTTP_200_OK, export.text
