"""Plan 20 S2 — the design doc's §2.3 permission table as an executable matrix.

Every role against every endpoint of the tool surface. The table is short enough
to state in prose and long enough to get wrong in code, which is what makes it
worth pinning: `ROLE_PERMISSIONS` does not inherit, so a permission omitted from
one role's set is a silent 403 nobody notices until an operator hits it.

Three properties here are not "does RBAC work" but specific mistakes this
codebase has already made:

- **the per-key tool policy reads and writes under the same domain.** Round 15's
  ISSUE-042 was per-key spend caps with the write under `keys:issue` and the read
  under `budget:read`, so an issuer could `PUT` an object and then be refused the
  `GET` of it. The test asserts the *symmetry*, not just the permission.
- **authorization precedes existence.** Every endpoint is probed with a random
  server id: a role without the permission must get 403 whether or not the
  resource exists, or the status code itself becomes an existence oracle.
- **another team's server is 404, not 403.** A 403 would confirm the resource
  exists, which is precisely what a tenant must not learn about another's
  registry.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

from .conftest import (
    ADMIN_EMAIL,
    MASTER_KEY,
    _admin,
    _bearer,
    _login,
    _member_token,
    _signup,
    _team,
)

# Resolvable and private, which the *allow*-list permits by design: it authorizes
# named targets rather than filtering by address class (that is the SSRF
# deny-list's job, on a different code path).
ALLOWED_HOST = "localhost:9443"
SERVER_URL = "https://localhost:9443/mcp"


@pytest.fixture
async def client(database_url: str) -> AsyncIterator[AsyncTestClient]:
    """Shadows the package fixture to opt this deployment into MCP egress.

    Without `MCP_ALLOWED_HOSTS` the feature is fail-closed and no server can be
    registered at all — correct, but it would make every write here a 400 and
    hide the permission behaviour the module is about.
    """
    settings = Settings(
        database_url=database_url,
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
        mcp_allowed_hosts=(ALLOWED_HOST,),
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _team_endpoints(team: str, server: str, key: str) -> list[tuple[str, str, str, dict | None]]:
    """(label, method, path, body) for every team-scoped tool endpoint."""
    return [
        ("list servers [tools:read]", "GET", f"/teams/{team}/mcp-servers", None),
        ("get server [tools:read]", "GET", f"/teams/{team}/mcp-servers/{server}", None),
        ("list tools [tools:read]", "GET", f"/teams/{team}/mcp-servers/{server}/tools", None),
        (
            "read key policy [tools:read]",
            "GET",
            f"/teams/{team}/keys/{key}/tool-policy",
            None,
        ),
        (
            "create server [tools:manage]",
            "POST",
            f"/teams/{team}/mcp-servers",
            {"name": "probe", "url": SERVER_URL},
        ),
        (
            "update server [tools:manage]",
            "PATCH",
            f"/teams/{team}/mcp-servers/{server}",
            {"enabled": False},
        ),
        ("remove server [tools:manage]", "DELETE", f"/teams/{team}/mcp-servers/{server}", None),
        (
            "reattach server [tools:manage]",
            "POST",
            f"/teams/{team}/mcp-servers/{server}/reattach",
            None,
        ),
        (
            "declare effect [tools:manage]",
            "PUT",
            f"/teams/{team}/mcp-servers/{server}/tools/delete_repo/effect",
            {"effect": "read"},
        ),
        (
            "write key policy [tools:manage]",
            "PUT",
            f"/teams/{team}/keys/{key}/tool-policy",
            {"destructive_enabled": True},
        ),
        (
            "clear key policy [tools:manage]",
            "DELETE",
            f"/teams/{team}/keys/{key}/tool-policy",
            None,
        ),
    ]


async def _call(
    client: AsyncTestClient, method: str, path: str, body: dict | None, token: str
) -> int:
    response = await client.request(method, path, json=body, headers=_bearer(token))
    return response.status_code


# ── the matrix ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["member", "model-manager", "key-issuer", "billing-viewer"])
async def test_no_role_but_admin_reaches_the_tool_surface(
    client: AsyncTestClient, role: str
) -> None:
    """The corrected §2.3 table: `tools:read` and `tools:manage` are the team
    admin's alone.

    `model-manager` is the interesting row. An earlier draft of the design gave it
    `tools:read` for convenience, and the RBAC tests refused it: each extended
    role grants exactly one capability domain, and reading a tool inventory is not
    the models domain. Attaching a tool server is an egress decision, which is why
    `guardrails:manage` is withheld from the same role.
    """
    admin = await _admin(client)
    team = await _team(client, admin)
    token = await _member_token(client, admin, team, f"{role}@corp.com", role)

    # A random id on purpose: the permission must be decided before the lookup,
    # so the refusal cannot double as an existence check.
    for label, method, path, body in _team_endpoints(team, str(uuid4()), str(uuid4())):
        status = await _call(client, method, path, body, token)
        assert status == HTTP_403_FORBIDDEN, f"{role} was not refused {label}: got {status}"


async def test_the_team_admin_reaches_every_endpoint(client: AsyncTestClient) -> None:
    """Authorization only: a 404 for a server that does not exist is a pass here.
    What must not appear anywhere is a 403."""
    admin = await _admin(client)
    team = await _team(client, admin)

    for label, method, path, body in _team_endpoints(team, str(uuid4()), str(uuid4())):
        status = await _call(client, method, path, body, admin)
        assert status != HTTP_403_FORBIDDEN, f"team admin was refused {label}"


async def test_the_platform_auditor_has_no_tool_access_at_all(client: AsyncTestClient) -> None:
    """The auditor's cross-team bypass is *strictly read-only billing
    visibility*. An earlier draft of §2.3 gave it an inventory-only view; a tool
    inventory is not billing, so the widening was dropped rather than shipped
    inside a feature."""
    admin = await _admin(client)
    team = await _team(client, admin)
    user_id = await _signup(client, admin, "aud-tools@corp.com")
    await client.patch(
        f"/users/{user_id}/auditor", json={"is_auditor": True}, headers=_bearer(admin)
    )
    token = await _login(client, "aud-tools@corp.com", "Sup3r-Secret!")

    for label, method, path, body in _team_endpoints(team, str(uuid4()), str(uuid4())):
        status = await _call(client, method, path, body, token)
        assert status == HTTP_403_FORBIDDEN, f"auditor was not refused {label}: got {status}"
    # ...including the platform surface, where it is not an admin either.
    assert (
        await client.get("/platform/mcp-servers", headers=_bearer(token))
    ).status_code == HTTP_403_FORBIDDEN


async def test_the_key_policy_read_and_write_share_one_permission_domain(
    client: AsyncTestClient,
) -> None:
    """ISSUE-042's regression, written before the fact.

    A key issuer holds `keys:issue` and can mint the very key in the path, so if
    the tool policy had landed under that permission this call would succeed —
    and the `GET`, under `tools:read`, would not. Both refuse, which is the
    symmetry the finding was about.
    """
    admin = await _admin(client)
    team = await _team(client, admin)
    issuer = await _member_token(client, admin, team, "ki-tools@corp.com", "key-issuer")
    minted = await client.post(f"/teams/{team}/keys", json={"name": "app"}, headers=_bearer(issuer))
    assert minted.status_code == HTTP_201_CREATED, minted.text
    key = minted.json()["id"]

    read = await client.get(f"/teams/{team}/keys/{key}/tool-policy", headers=_bearer(issuer))
    write = await client.put(
        f"/teams/{team}/keys/{key}/tool-policy",
        json={"destructive_enabled": True},
        headers=_bearer(issuer),
    )

    assert read.status_code == HTTP_403_FORBIDDEN
    assert write.status_code == HTTP_403_FORBIDDEN


# ── the proposal surface (Plan 20 S5) ────────────────────────────────────────
#
# The one place in this table where a non-admin role is *supposed* to reach
# something. `tools:propose` is held by every team role, which `ROLE_PERMISSIONS`
# cannot express by inheritance — each role's set is exact — so the matrix below is
# the only thing that would catch a role quietly missing it.


def _propose_endpoints(team: str, proposal: str) -> list[tuple[str, str, str, dict | None]]:
    return [
        ("list proposals [tools:propose]", "GET", f"/teams/{team}/mcp-server-proposals", None),
        (
            "file proposal [tools:propose]",
            "POST",
            f"/teams/{team}/mcp-server-proposals",
            {"name": "probe", "url": SERVER_URL},
        ),
    ]


def _decide_endpoints(team: str, proposal: str) -> list[tuple[str, str, str, dict | None]]:
    return [
        (
            "approve [tools:manage]",
            "POST",
            f"/teams/{team}/mcp-server-proposals/{proposal}/approve",
            None,
        ),
        (
            "reject [tools:manage]",
            "POST",
            f"/teams/{team}/mcp-server-proposals/{proposal}/reject",
            {"reason": "no"},
        ),
    ]


@pytest.mark.parametrize("role", ["member", "model-manager", "key-issuer", "billing-viewer"])
async def test_every_team_role_may_ask_and_none_may_decide(
    client: AsyncTestClient, role: str
) -> None:
    """The asymmetry the slice exists for, as a matrix row.

    A role that could decide its own proposal would make the flow an escalation
    with extra steps; a role that could not ask would put the person who knows
    which tool the application needs behind the person who holds the permission.
    """
    admin = await _admin(client)
    team = await _team(client, admin)
    token = await _member_token(client, admin, team, f"{role}-p@corp.com", role)

    for label, method, path, body in _propose_endpoints(team, str(uuid4())):
        status = await _call(client, method, path, body, token)
        assert status != HTTP_403_FORBIDDEN, f"{role} was refused {label}: got {status}"
    # A random proposal id on purpose: the permission must be decided before the
    # lookup, so a 403 here cannot double as an existence check.
    for label, method, path, body in _decide_endpoints(team, str(uuid4())):
        status = await _call(client, method, path, body, token)
        assert status == HTTP_403_FORBIDDEN, f"{role} reached {label}: got {status}"


async def test_the_platform_auditor_cannot_even_propose(client: AsyncTestClient) -> None:
    """The auditor's cross-team bypass is billing visibility, and it is read-only.
    Filing a proposal is a write, and one an admin may act on."""
    admin = await _admin(client)
    team = await _team(client, admin)
    user_id = await _signup(client, admin, "aud-prop@corp.com")
    await client.patch(
        f"/users/{user_id}/auditor", json={"is_auditor": True}, headers=_bearer(admin)
    )
    token = await _login(client, "aud-prop@corp.com", "Sup3r-Secret!")

    for label, method, path, body in _propose_endpoints(team, str(uuid4())) + _decide_endpoints(
        team, str(uuid4())
    ):
        status = await _call(client, method, path, body, token)
        assert status == HTTP_403_FORBIDDEN, f"auditor was not refused {label}: got {status}"


async def test_an_anonymous_caller_cannot_propose(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)

    for method, path, body in (
        ("GET", f"/teams/{team}/mcp-server-proposals", None),
        ("POST", f"/teams/{team}/mcp-server-proposals", {"name": "x", "url": SERVER_URL}),
        ("POST", f"/teams/{team}/mcp-server-proposals/{uuid4()}/approve", None),
    ):
        response = await client.request(method, path, json=body)
        assert response.status_code in (401, 403), (
            f"{method} {path} was open: {response.status_code}"
        )


# ── tenancy ──────────────────────────────────────────────────────────────────


async def test_another_teams_server_is_not_found_rather_than_forbidden(
    client: AsyncTestClient,
) -> None:
    admin = await _admin(client)
    owning_team = await _team(client, admin)
    other_team = await _team(client, admin)
    created = await client.post(
        f"/teams/{owning_team}/mcp-servers",
        json={"name": "github", "url": SERVER_URL},
        headers=_bearer(admin),
    )
    assert created.status_code == HTTP_201_CREATED, created.text
    server = created.json()["id"]

    # The admin here is a platform admin, so this is not a permission refusal:
    # the server is genuinely outside the team named in the path.
    seen = await client.get(f"/teams/{other_team}/mcp-servers/{server}", headers=_bearer(admin))

    assert seen.status_code == HTTP_404_NOT_FOUND
    assert (
        await client.get(f"/teams/{other_team}/mcp-servers", headers=_bearer(admin))
    ).json() == []


async def test_a_team_admin_of_another_team_gets_404_not_403(client: AsyncTestClient) -> None:
    """The same property for a real team admin rather than the platform one:
    they hold `tools:read` in their own team, so a 403 would have to come from the
    tenancy check — and it must not, because that would confirm the id exists."""
    admin = await _admin(client)
    owning_team = await _team(client, admin)
    other_team = await _team(client, admin)
    created = await client.post(
        f"/teams/{owning_team}/mcp-servers",
        json={"name": "github", "url": SERVER_URL},
        headers=_bearer(admin),
    )
    server = created.json()["id"]
    outsider = await _member_token(client, admin, other_team, "ta@corp.com", "admin")

    seen = await client.get(f"/teams/{other_team}/mcp-servers/{server}", headers=_bearer(outsider))

    assert seen.status_code == HTTP_404_NOT_FOUND
    # ...and asking inside the team that owns it is a permission failure, since
    # this admin is not a member there. 403 here is correct: the caller is not
    # being told anything about the resource, only about their own membership.
    assert (
        await client.get(f"/teams/{owning_team}/mcp-servers/{server}", headers=_bearer(outsider))
    ).status_code == HTTP_403_FORBIDDEN


# ── the platform surface ─────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["admin", "member", "model-manager"])
async def test_only_a_platform_admin_reaches_the_platform_surface(
    client: AsyncTestClient, role: str
) -> None:
    """Including a *team* admin: `tools:manage` is a permission inside one team,
    and promoting a server to global is a decision about every team."""
    platform_admin = await _admin(client)
    team = await _team(client, platform_admin)
    token = await _member_token(client, platform_admin, team, f"plat-{role}@corp.com", role)
    server = str(uuid4())

    for method, path, body in (
        ("GET", "/platform/mcp-servers", None),
        ("POST", "/platform/mcp-servers", {"name": "g", "url": SERVER_URL}),
        ("PATCH", f"/platform/mcp-servers/{server}", {"enabled": False}),
        ("DELETE", f"/platform/mcp-servers/{server}", None),
        ("POST", f"/platform/mcp-servers/{server}/make-global", None),
        ("POST", f"/platform/mcp-servers/{server}/extend", {"team_ids": [team]}),
        ("GET", f"/platform/mcp-servers/{server}/grants", None),
        ("DELETE", f"/platform/mcp-servers/grants/{uuid4()}", None),
    ):
        status = await _call(client, method, path, body, token)
        assert status == HTTP_403_FORBIDDEN, f"{role} reached {method} {path}: got {status}"


async def test_a_platform_admin_bypasses_the_team_permission_check(
    client: AsyncTestClient,
) -> None:
    """Existing behaviour, asserted for the new surface rather than assumed: the
    platform admin is not a member of the team it is administering."""
    admin = await _admin(client)
    team = await _team(client, admin)

    created = await client.post(
        f"/teams/{team}/mcp-servers",
        json={"name": "github", "url": SERVER_URL, "auth": "t0ken"},
        headers=_bearer(admin),
    )

    assert created.status_code == HTTP_201_CREATED, created.text
    assert created.json()["has_auth"] is True
    assert "t0ken" not in created.text  # the token is not readable back
    listed = await client.get(f"/teams/{team}/mcp-servers", headers=_bearer(admin))
    assert [s["name"] for s in listed.json()] == ["github"]
    assert (
        await client.delete(
            f"/teams/{team}/mcp-servers/{created.json()['id']}", headers=_bearer(admin)
        )
    ).json() == {"outcome": "deleted"}


async def test_an_anonymous_caller_reaches_nothing(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)

    for method, path, body in (
        ("GET", f"/teams/{team}/mcp-servers", None),
        ("POST", f"/teams/{team}/mcp-servers", {"name": "x", "url": SERVER_URL}),
        ("GET", "/platform/mcp-servers", None),
    ):
        response = await client.request(method, path, json=body)
        assert response.status_code in (401, 403), (
            f"{method} {path} was open: {response.status_code}"
        )


# ── the per-key policy's own behaviour ───────────────────────────────────────


async def test_a_key_without_a_policy_reports_unrestricted_rather_than_404(
    client: AsyncTestClient,
) -> None:
    """Absent means unrestricted — the polarity a missing spend cap already has.
    A 404 would suggest something is missing, when in fact the key can call every
    non-destructive tool."""
    admin = await _admin(client)
    team = await _team(client, admin)
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "app"}, headers=_bearer(admin))
    ).json()["id"]

    policy = await client.get(f"/teams/{team}/keys/{key}/tool-policy", headers=_bearer(admin))

    assert policy.status_code == HTTP_200_OK, policy.text
    assert policy.json() == {
        "api_key_id": key,
        "restricted": False,
        "destructive_enabled": False,
        "allowed_tools": [],
        "created_at": None,
    }


async def test_setting_a_policy_twice_replaces_it(client: AsyncTestClient) -> None:
    """`PUT` semantics: the second call must not trip the one-row-per-key unique
    constraint, which an insert-only adapter would."""
    admin = await _admin(client)
    team = await _team(client, admin)
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "app"}, headers=_bearer(admin))
    ).json()["id"]
    path = f"/teams/{team}/keys/{key}/tool-policy"

    first = await client.put(path, json={"allowed_tools": ["search"]}, headers=_bearer(admin))
    second = await client.put(
        path,
        json={"allowed_tools": ["search", "fetch"], "destructive_enabled": True},
        headers=_bearer(admin),
    )

    assert first.status_code == HTTP_200_OK, first.text
    assert second.status_code == HTTP_200_OK, second.text
    assert second.json()["allowed_tools"] == ["search", "fetch"]
    assert second.json()["destructive_enabled"] is True
    assert (await client.delete(path, headers=_bearer(admin))).status_code == HTTP_204_NO_CONTENT
    assert (await client.get(path, headers=_bearer(admin))).json()["restricted"] is False


async def test_a_key_from_another_team_is_404(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    owning_team = await _team(client, admin)
    other_team = await _team(client, admin)
    key = (
        await client.post(
            f"/teams/{owning_team}/keys", json={"name": "app"}, headers=_bearer(admin)
        )
    ).json()["id"]

    crossed = await client.put(
        f"/teams/{other_team}/keys/{key}/tool-policy",
        json={"destructive_enabled": True},
        headers=_bearer(admin),
    )

    assert crossed.status_code == HTTP_404_NOT_FOUND
