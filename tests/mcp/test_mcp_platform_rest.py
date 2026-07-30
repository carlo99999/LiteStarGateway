"""Plan 20 S2 — the platform surface end to end, and the asymmetry it must keep.

The team surface and this one differ on purpose in one place: `DELETE` here
really deletes, while the team's detaches. That split is the whole reason Round
12's ISSUE-020 cannot repeat — a team admin removing a shared server must not
revoke it from every other tenant — so the test that matters most is the pair of
them side by side.

The other property worth pinning is that a platform admin is exempt from the
*team permission check*, not from `MCP_ALLOWED_HOSTS`. The allowlist bounds where
the gateway process may connect, which is a deployment fact rather than a tenancy
one, and "the platform admin can bypass it" would make the veto decorative.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
)
from litestar.testing import AsyncTestClient
from rbac.conftest import (  # type: ignore[import-not-found]
    ADMIN_EMAIL,
    MASTER_KEY,
    _admin,
    _bearer,
    _team,
)

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

SERVER_URL = "https://localhost:9443/mcp"
OTHER_URL = "https://localhost:9443/other"


@pytest.fixture
async def client(database_url: str) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=database_url,
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
        mcp_allowed_hosts=("localhost:9443",),
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


async def _server(client: AsyncTestClient, admin: str, team: str, name: str, url=SERVER_URL) -> str:
    created = await client.post(
        f"/teams/{team}/mcp-servers", json={"name": name, "url": url}, headers=_bearer(admin)
    )
    assert created.status_code == HTTP_201_CREATED, created.text
    return created.json()["id"]


# ── global servers ───────────────────────────────────────────────────────────


async def test_a_global_server_is_visible_to_every_team_without_a_grant(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    first = await _team(client, admin)
    second = await _team(client, admin)

    created = await client.post(
        "/platform/mcp-servers",
        json={"name": "shared", "url": SERVER_URL, "auth": "t0ken"},
        headers=_bearer(admin),
    )

    assert created.status_code == HTTP_201_CREATED, created.text
    assert created.json()["origin"] == "global"
    assert created.json()["has_auth"] is True
    assert "t0ken" not in created.text
    for team in (first, second):
        listed = await client.get(f"/teams/{team}/mcp-servers", headers=_bearer(admin))
        assert [(s["name"], s["origin"]) for s in listed.json()] == [("shared", "global")]


async def test_the_allowlist_binds_the_platform_admin_too(client: AsyncTestClient) -> None:
    """Exempt from the team check, not from the egress veto."""
    admin = await _admin(client)

    off_list = await client.post(
        "/platform/mcp-servers",
        json={"name": "evil", "url": "https://attacker.example/mcp"},
        headers=_bearer(admin),
    )
    # A resolvable host on a port nobody authorized: same veto, and the branch
    # whose message an operator has to act on. (The name above may not resolve at
    # all on an isolated machine, which is a different — also refused — path.)
    wrong_port = await client.post(
        "/platform/mcp-servers",
        json={"name": "evil2", "url": "https://localhost:9999/mcp"},
        headers=_bearer(admin),
    )

    assert off_list.status_code == HTTP_400_BAD_REQUEST
    assert wrong_port.status_code == HTTP_400_BAD_REQUEST
    # The message names the variable an operator would actually have to edit,
    # rather than the provider allowlist the shared resolver mentions.
    assert "MCP_ALLOWED_HOSTS" in wrong_port.text
    assert "OPENAI_COMPATIBLE" not in wrong_port.text
    assert (await client.get("/platform/mcp-servers", headers=_bearer(admin))).json() == []


async def test_promotion_makes_a_team_server_global_and_drops_its_grants(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    owner = await _team(client, admin)
    guest = await _team(client, admin)
    server = await _server(client, admin, owner, "github")
    await client.post(
        f"/platform/mcp-servers/{server}/extend",
        json={"team_ids": [guest]},
        headers=_bearer(admin),
    )

    promoted = await client.post(
        f"/platform/mcp-servers/{server}/make-global", headers=_bearer(admin)
    )

    assert promoted.status_code == HTTP_201_CREATED, promoted.text
    assert promoted.json()["origin"] == "global"
    assert promoted.json()["team_id"] is None
    # The grant is gone rather than left as a row nothing reads: a global server
    # resolves to every team by itself.
    grants = await client.get(f"/platform/mcp-servers/{server}/grants", headers=_bearer(admin))
    assert grants.json() == []
    # A third team, never granted anything, still sees it.
    third = await _team(client, admin)
    listed = await client.get(f"/teams/{third}/mcp-servers", headers=_bearer(admin))
    assert [s["origin"] for s in listed.json()] == ["global"]


async def test_promoting_twice_is_refused_rather_than_a_silent_no_op(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    server = await _server(client, admin, team, "github")
    await client.post(f"/platform/mcp-servers/{server}/make-global", headers=_bearer(admin))

    again = await client.post(f"/platform/mcp-servers/{server}/make-global", headers=_bearer(admin))

    assert again.status_code == HTTP_400_BAD_REQUEST
    assert "already global" in again.text


async def test_a_name_another_team_already_uses_cannot_become_global(
    client: AsyncTestClient,
) -> None:
    """A server is referenced by its name and there is no alias, so a global
    "github" alongside a team's own "github" would put two servers under one name
    in front of that team. Refused here rather than resolved silently later."""
    admin = await _admin(client)
    first = await _team(client, admin)
    second = await _team(client, admin)
    server = await _server(client, admin, first, "github")
    await _server(client, admin, second, "github", url=OTHER_URL)

    refused = await client.post(
        f"/platform/mcp-servers/{server}/make-global", headers=_bearer(admin)
    )

    assert refused.status_code == HTTP_400_BAD_REQUEST
    assert "already named" in refused.text


# ── extension grants ─────────────────────────────────────────────────────────


async def test_extending_shares_the_same_server_and_un_extending_takes_it_back(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    owner = await _team(client, admin)
    guest = await _team(client, admin)
    server = await _server(client, admin, owner, "github")

    extended = await client.post(
        f"/platform/mcp-servers/{server}/extend",
        json={"team_ids": [guest]},
        headers=_bearer(admin),
    )

    assert extended.status_code == HTTP_201_CREATED, extended.text
    grant = extended.json()[0]
    assert grant["team_id"] == guest
    listed = await client.get(f"/teams/{guest}/mcp-servers", headers=_bearer(admin))
    assert [(s["name"], s["origin"]) for s in listed.json()] == [("github", "extended")]

    revoked = await client.delete(
        f"/platform/mcp-servers/grants/{grant['id']}", headers=_bearer(admin)
    )

    assert revoked.status_code == HTTP_204_NO_CONTENT
    assert (await client.get(f"/teams/{guest}/mcp-servers", headers=_bearer(admin))).json() == []
    # ...and the source is untouched: a grant is a share, not a copy.
    owner_view = await client.get(f"/teams/{owner}/mcp-servers", headers=_bearer(admin))
    assert [s["origin"] for s in owner_view.json()] == ["own"]


async def test_extending_a_global_server_is_refused(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    created = await client.post(
        "/platform/mcp-servers", json={"name": "shared", "url": SERVER_URL}, headers=_bearer(admin)
    )

    refused = await client.post(
        f"/platform/mcp-servers/{created.json()['id']}/extend",
        json={"team_ids": [team]},
        headers=_bearer(admin),
    )

    assert refused.status_code == HTTP_400_BAD_REQUEST
    assert "already available to every team" in refused.text


async def test_extending_to_a_team_that_already_sees_that_name_is_refused(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    owner = await _team(client, admin)
    guest = await _team(client, admin)
    server = await _server(client, admin, owner, "github")
    await _server(client, admin, guest, "github", url=OTHER_URL)

    refused = await client.post(
        f"/platform/mcp-servers/{server}/extend",
        json={"team_ids": [guest]},
        headers=_bearer(admin),
    )

    assert refused.status_code == HTTP_400_BAD_REQUEST
    assert "already sees a server named" in refused.text


async def test_extending_to_the_owning_team_is_refused(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    owner = await _team(client, admin)
    server = await _server(client, admin, owner, "github")

    refused = await client.post(
        f"/platform/mcp-servers/{server}/extend",
        json={"team_ids": [owner]},
        headers=_bearer(admin),
    )

    assert refused.status_code == HTTP_400_BAD_REQUEST
    assert (
        await client.get(f"/platform/mcp-servers/{server}/grants", headers=_bearer(admin))
    ).json() == []


async def test_re_extending_after_a_detach_makes_the_server_visible_again(
    client: AsyncTestClient,
) -> None:
    """The detach was a choice about the previous grant. Granting again is a new
    decision by a privileged actor, so the stale suppression must not silently
    win — otherwise the platform admin's action appears to do nothing."""
    admin = await _admin(client)
    owner = await _team(client, admin)
    guest = await _team(client, admin)
    server = await _server(client, admin, owner, "github")
    await client.post(
        f"/platform/mcp-servers/{server}/extend",
        json={"team_ids": [guest]},
        headers=_bearer(admin),
    )
    detached = await client.delete(f"/teams/{guest}/mcp-servers/{server}", headers=_bearer(admin))
    assert detached.json() == {"outcome": "detached"}

    await client.post(
        f"/platform/mcp-servers/{server}/extend",
        json={"team_ids": [guest]},
        headers=_bearer(admin),
    )

    listed = await client.get(f"/teams/{guest}/mcp-servers", headers=_bearer(admin))
    assert [s["name"] for s in listed.json()] == ["github"]


# ── the delete/detach asymmetry ──────────────────────────────────────────────


async def test_a_team_detaches_a_global_server_and_the_others_keep_it(
    client: AsyncTestClient,
) -> None:
    """ISSUE-020's shape end to end: one tenant removing a shared capability must
    not remove it for the rest."""
    admin = await _admin(client)
    first = await _team(client, admin)
    second = await _team(client, admin)
    created = await client.post(
        "/platform/mcp-servers", json={"name": "shared", "url": SERVER_URL}, headers=_bearer(admin)
    )
    server = created.json()["id"]

    removed = await client.delete(f"/teams/{first}/mcp-servers/{server}", headers=_bearer(admin))

    assert removed.status_code == HTTP_200_OK
    assert removed.json() == {"outcome": "detached"}
    assert (await client.get(f"/teams/{first}/mcp-servers", headers=_bearer(admin))).json() == []
    assert [
        s["name"]
        for s in (await client.get(f"/teams/{second}/mcp-servers", headers=_bearer(admin))).json()
    ] == ["shared"]
    # The resource itself is untouched — the platform still lists it.
    assert [
        s["name"]
        for s in (await client.get("/platform/mcp-servers", headers=_bearer(admin))).json()
    ] == ["shared"]

    reattached = await client.post(
        f"/teams/{first}/mcp-servers/{server}/reattach", headers=_bearer(admin)
    )

    assert reattached.status_code == HTTP_200_OK, reattached.text
    assert [
        s["name"]
        for s in (await client.get(f"/teams/{first}/mcp-servers", headers=_bearer(admin))).json()
    ] == ["shared"]


async def test_the_platform_delete_removes_the_resource_for_everyone(
    client: AsyncTestClient,
) -> None:
    """The counterpart: this verb is the one that really deletes, which is why it
    lives behind a platform admin and not behind `tools:manage`."""
    admin = await _admin(client)
    first = await _team(client, admin)
    second = await _team(client, admin)
    created = await client.post(
        "/platform/mcp-servers", json={"name": "shared", "url": SERVER_URL}, headers=_bearer(admin)
    )
    server = created.json()["id"]

    deleted = await client.delete(f"/platform/mcp-servers/{server}", headers=_bearer(admin))

    assert deleted.status_code == HTTP_204_NO_CONTENT
    for team in (first, second):
        assert (await client.get(f"/teams/{team}/mcp-servers", headers=_bearer(admin))).json() == []
    assert (await client.get("/platform/mcp-servers", headers=_bearer(admin))).json() == []
    assert (
        await client.delete(f"/platform/mcp-servers/{server}", headers=_bearer(admin))
    ).status_code == HTTP_404_NOT_FOUND


async def test_a_team_cannot_edit_a_server_it_only_sees(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    created = await client.post(
        "/platform/mcp-servers", json={"name": "shared", "url": SERVER_URL}, headers=_bearer(admin)
    )
    server = created.json()["id"]

    refused = await client.patch(
        f"/teams/{team}/mcp-servers/{server}",
        json={"enabled": False},
        headers=_bearer(admin),
    )

    assert refused.status_code == HTTP_400_BAD_REQUEST
    assert "not edited here" in refused.text
    # ...while the platform can, on the same resource.
    updated = await client.patch(
        f"/platform/mcp-servers/{server}", json={"enabled": False}, headers=_bearer(admin)
    )
    assert updated.status_code == HTTP_200_OK, updated.text
    assert updated.json()["enabled"] is False


async def test_an_unknown_server_is_404_on_every_platform_route(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    from uuid import uuid4

    missing = uuid4()

    for method, path, body in (
        ("PATCH", f"/platform/mcp-servers/{missing}", {"enabled": False}),
        ("DELETE", f"/platform/mcp-servers/{missing}", None),
        ("POST", f"/platform/mcp-servers/{missing}/make-global", None),
        ("GET", f"/platform/mcp-servers/{missing}/grants", None),
        ("DELETE", f"/platform/mcp-servers/grants/{missing}", None),
    ):
        response = await client.request(method, path, json=body, headers=_bearer(admin))
        assert response.status_code == HTTP_404_NOT_FOUND, (
            f"{method} {path}: {response.status_code}"
        )
