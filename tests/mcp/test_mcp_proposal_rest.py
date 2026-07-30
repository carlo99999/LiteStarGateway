"""Plan 20 S5 — the proposal flow end to end, on the wired path.

The service tests pin the rules with doubles. This module asserts the thing they
cannot: that the *deployed* wiring behaves that way — the real controller, the real
dependency graph, the real discovery client. Round 15's recurring shape was a
control that held in the service and not on some other path reaching it.

The two properties worth the round trip:

**A plain `member` can file and cannot decide.** That asymmetry is the whole
feature. `member` is refused every endpoint of the server surface (see
`tests/rbac/test_mcp_rbac.py`) and reaches this one, because asking is not
managing.

**Filing makes no outbound request.** Patched at the client class rather than
injected, so what is being observed is the object the app actually built.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from _invite_helpers import issue_invite  # type: ignore[import-not-found]
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
)
from litestar.testing import AsyncTestClient
from rbac.conftest import (  # type: ignore[import-not-found]
    ADMIN_EMAIL,
    MASTER_KEY,
    _admin,
    _bearer,
    _login,
    _member_token,
    _team,
)

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings
from litestar_gateway.domain.mcp import McpServer, McpTool, ToolEffect

SERVER_URL = "https://localhost:9443/mcp"


class Discoveries:
    """Every `tools/list` the gateway made, in order."""

    def __init__(self) -> None:
        self.urls: list[str] = []


@pytest.fixture
def discoveries(monkeypatch: pytest.MonkeyPatch) -> Discoveries:
    """Patched on the client class, so what is observed is the object the app
    actually built — not one the test injected past the wiring."""
    from litestar_gateway.infrastructure.mcp.client import McpDiscoveryClient

    recorder = Discoveries()

    async def list_tools(
        self: McpDiscoveryClient, server: McpServer, *, auth: str | None = None
    ) -> list[McpTool]:
        recorder.urls.append(server.url)
        return [
            McpTool(
                id=uuid4(),
                server_id=server.id,
                name="search",
                description="",
                schema={"type": "object"},
                effect=ToolEffect.READ,
            )
        ]

    monkeypatch.setattr(McpDiscoveryClient, "list_tools", list_tools)
    return recorder


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


async def _file(
    client: AsyncTestClient, token: str, team: str, name: str = "github", **extra
) -> dict:
    filed = await client.post(
        f"/teams/{team}/mcp-server-proposals",
        json={"name": name, "url": SERVER_URL, **extra},
        headers=_bearer(token),
    )
    assert filed.status_code == HTTP_201_CREATED, filed.text
    return filed.json()


# ── the member's side ────────────────────────────────────────────────────────


async def test_a_member_can_file_a_proposal_but_not_register_a_server(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    """The asymmetry the whole slice exists for: `tools:propose` in every role,
    `tools:manage` in none but admin."""
    admin = await _admin(client)
    team = await _team(client, admin)
    member = await _member_token(client, admin, team, "m@corp.com", "member")

    proposal = await _file(client, member, team)

    assert proposal["status"] == "pending"
    # ...and the same member cannot register one directly.
    refused = await client.post(
        f"/teams/{team}/mcp-servers",
        json={"name": "direct", "url": SERVER_URL},
        headers=_bearer(member),
    )
    assert refused.status_code == HTTP_403_FORBIDDEN
    # Nothing was registered by filing, and nothing was contacted.
    assert (await client.get(f"/teams/{team}/mcp-servers", headers=_bearer(admin))).json() == []
    assert discoveries.urls == []


@pytest.mark.parametrize("role", ["member", "model-manager", "key-issuer", "billing-viewer"])
async def test_every_team_role_may_propose(
    client: AsyncTestClient, discoveries: Discoveries, role: str
) -> None:
    """`ROLE_PERMISSIONS` does not inherit, so "any member of the team may ask"
    has to be spelled out per role — and a role missing from that list is a silent
    403 nobody notices until an operator hits it."""
    admin = await _admin(client)
    team = await _team(client, admin)
    token = await _member_token(client, admin, team, f"{role}-prop@corp.com", role)

    filed = await client.post(
        f"/teams/{team}/mcp-server-proposals",
        json={"name": f"srv-{role}", "url": SERVER_URL},
        headers=_bearer(token),
    )

    assert filed.status_code == HTTP_201_CREATED, f"{role} could not propose: {filed.text}"


async def test_filing_makes_no_outbound_request_on_the_wired_path(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    """The design's rule that no member action causes gateway egress, asserted
    against the client the app actually constructed."""
    admin = await _admin(client)
    team = await _team(client, admin)
    member = await _member_token(client, admin, team, "quiet@corp.com", "member")

    await _file(client, member, team, auth="pr0posed")
    await client.get(f"/teams/{team}/mcp-server-proposals", headers=_bearer(member))

    assert discoveries.urls == []


async def test_a_proposal_never_returns_its_token(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)

    filed = await client.post(
        f"/teams/{team}/mcp-server-proposals",
        json={"name": "github", "url": SERVER_URL, "auth": "pr0posed-token"},
        headers=_bearer(admin),
    )
    queue = await client.get(f"/teams/{team}/mcp-server-proposals", headers=_bearer(admin))

    assert filed.json()["has_auth"] is True
    assert "pr0posed-token" not in filed.text
    assert "pr0posed-token" not in queue.text


async def test_a_member_reads_the_decision_and_the_reason(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    """A refused proposal that simply disappeared is no answer for the person who
    asked, which is why the queue is readable under `tools:propose` and a rejection
    carries why."""
    admin = await _admin(client)
    team = await _team(client, admin)
    member = await _member_token(client, admin, team, "asker@corp.com", "member")
    proposal = await _file(client, member, team)

    rejected = await client.post(
        f"/teams/{team}/mcp-server-proposals/{proposal['id']}/reject",
        json={"reason": "use the global github server"},
        headers=_bearer(admin),
    )
    seen = await client.get(f"/teams/{team}/mcp-server-proposals", headers=_bearer(member))

    assert rejected.status_code == HTTP_200_OK, rejected.text
    assert seen.json()[0]["status"] == "rejected"
    assert seen.json()[0]["reason"] == "use the global github server"
    assert seen.json()[0]["server_id"] is None


async def test_a_member_cannot_decide_its_own_proposal(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    """Otherwise the flow is a permission escalation with extra steps."""
    admin = await _admin(client)
    team = await _team(client, admin)
    member = await _member_token(client, admin, team, "self@corp.com", "member")
    proposal = await _file(client, member, team)

    approve = await client.post(
        f"/teams/{team}/mcp-server-proposals/{proposal['id']}/approve", headers=_bearer(member)
    )
    reject = await client.post(
        f"/teams/{team}/mcp-server-proposals/{proposal['id']}/reject",
        json={"reason": "mine now"},
        headers=_bearer(member),
    )

    assert approve.status_code == HTTP_403_FORBIDDEN
    assert reject.status_code == HTTP_403_FORBIDDEN
    assert discoveries.urls == []


# ── the approver's side ──────────────────────────────────────────────────────


async def test_approval_registers_the_server_and_discovers_once(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    member = await _member_token(client, admin, team, "m2@corp.com", "member")
    proposal = await _file(client, member, team, auth="pr0posed")

    approved = await client.post(
        f"/teams/{team}/mcp-server-proposals/{proposal['id']}/approve", headers=_bearer(admin)
    )

    assert approved.status_code == HTTP_201_CREATED, approved.text
    assert approved.json()["name"] == "github"
    assert approved.json()["origin"] == "own"
    assert approved.json()["has_auth"] is True
    assert "pr0posed" not in approved.text
    # Discovery happened here and nowhere earlier: exactly one request, made after
    # a privileged actor decided the target was legitimate.
    assert discoveries.urls == [SERVER_URL]
    listed = await client.get(f"/teams/{team}/mcp-servers", headers=_bearer(admin))
    assert [server["name"] for server in listed.json()] == ["github"]
    inventory = await client.get(
        f"/teams/{team}/mcp-servers/{approved.json()['id']}/tools", headers=_bearer(admin)
    )
    assert [tool["name"] for tool in inventory.json()] == ["search"]
    queue = await client.get(f"/teams/{team}/mcp-server-proposals", headers=_bearer(admin))
    assert queue.json()[0]["status"] == "approved"
    assert queue.json()[0]["server_id"] == approved.json()["id"]


async def test_deciding_a_decided_proposal_is_a_conflict_not_a_second_server(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    """What the second admin sees when it loses the race, and what it does not
    cause: a duplicate server."""
    admin = await _admin(client)
    team = await _team(client, admin)
    proposal = await _file(client, admin, team)
    await client.post(
        f"/teams/{team}/mcp-server-proposals/{proposal['id']}/approve", headers=_bearer(admin)
    )

    again = await client.post(
        f"/teams/{team}/mcp-server-proposals/{proposal['id']}/approve", headers=_bearer(admin)
    )
    rejected = await client.post(
        f"/teams/{team}/mcp-server-proposals/{proposal['id']}/reject",
        json={"reason": "too late"},
        headers=_bearer(admin),
    )

    assert again.status_code == HTTP_409_CONFLICT
    assert rejected.status_code == HTTP_409_CONFLICT
    listed = await client.get(f"/teams/{team}/mcp-servers", headers=_bearer(admin))
    assert len(listed.json()) == 1
    assert discoveries.urls == [SERVER_URL]


async def test_a_rejection_without_a_reason_is_refused(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    proposal = await _file(client, admin, team)

    blank = await client.post(
        f"/teams/{team}/mcp-server-proposals/{proposal['id']}/reject",
        json={"reason": "  "},
        headers=_bearer(admin),
    )

    assert blank.status_code == HTTP_400_BAD_REQUEST
    # Still pending, so the approver can answer properly rather than having half
    # decided it.
    queue = await client.get(f"/teams/{team}/mcp-server-proposals", headers=_bearer(admin))
    assert queue.json()[0]["status"] == "pending"


async def test_the_pending_filter_is_the_queue(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    admin = await _admin(client)
    team = await _team(client, admin)
    keep = await _file(client, admin, team, name="github")
    drop = await _file(client, admin, team, name="jira")
    await client.post(
        f"/teams/{team}/mcp-server-proposals/{drop['id']}/reject",
        json={"reason": "not needed"},
        headers=_bearer(admin),
    )

    pending = await client.get(
        f"/teams/{team}/mcp-server-proposals?pending=true", headers=_bearer(admin)
    )

    assert [proposal["id"] for proposal in pending.json()] == [keep["id"]]


# ── tenancy and the allowlist ────────────────────────────────────────────────


async def test_another_teams_proposal_is_404(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    admin = await _admin(client)
    owning = await _team(client, admin)
    other = await _team(client, admin)
    proposal = await _file(client, admin, owning)

    crossed = await client.post(
        f"/teams/{other}/mcp-server-proposals/{proposal['id']}/approve", headers=_bearer(admin)
    )

    assert crossed.status_code == HTTP_404_NOT_FOUND
    assert (
        await client.get(f"/teams/{other}/mcp-server-proposals", headers=_bearer(admin))
    ).json() == []
    assert discoveries.urls == []


async def test_deleting_a_user_who_filed_a_proposal_keeps_the_proposal(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    """`mcp_server_proposal.proposed_by` is cleared by the database, not guarded by
    `delete_user` — the choice `tests/misc/test_user_fk_invariant.py` records.

    Two claims are being separated here. That the FK declares `ON DELETE SET NULL`
    is schema, checked there. That deleting the user then *works* is behaviour, and
    it is the one that breaks: a missing `ondelete` turns
    `DELETE /users/{id}` into an unhandled IntegrityError, which is ISSUE-008's
    shape and the reason that guard exists at all.
    """
    admin = await _admin(client)
    team = await _team(client, admin)
    # Invited into *this* team rather than through `_signup`'s helper, which seeds a
    # team of its own: the user must end up with exactly one membership, or the
    # delete below is refused for a reason that has nothing to do with proposals.
    # The proposal also has to be filed *by that user* — filing as the admin would
    # point `proposed_by` elsewhere and exercise nothing.
    invite = await issue_invite(client, admin, team, role="member")
    signed_up = await client.post(
        "/signup",
        json={
            "invite_token": invite,
            "email": "leaver@corp.com",
            "password": "Sup3r-Secret!",  # pragma: allowlist secret
        },
    )
    assert signed_up.status_code == HTTP_201_CREATED, signed_up.text
    leaver = signed_up.json()["id"]
    token = await _login(client, "leaver@corp.com", "Sup3r-Secret!")
    proposal = await _file(client, token, team)
    assert proposal["proposed_by"] == leaver

    # The membership itself blocks the delete (UserHasReferences), so it goes first
    # — the proposal must not be a second, unremovable reason.
    removed = await client.delete(f"/teams/{team}/members/{leaver}", headers=_bearer(admin))
    assert removed.status_code in (HTTP_200_OK, 204), removed.text
    deleted = await client.delete(f"/users/{leaver}", headers=_bearer(admin))

    assert deleted.status_code in (HTTP_200_OK, 204), deleted.text
    # The record of the request survives its author, with the link cleared.
    queue = await client.get(f"/teams/{team}/mcp-server-proposals", headers=_bearer(admin))
    assert [p["id"] for p in queue.json()] == [proposal["id"]]
    assert queue.json()[0]["proposed_by"] is None


async def test_a_proposal_cannot_name_a_host_outside_the_allowlist(
    client: AsyncTestClient, discoveries: Discoveries
) -> None:
    """The veto binds the lowest-privilege write too, and it binds it *offline*:
    the refusal is the allowlist, not a failed connection."""
    admin = await _admin(client)
    team = await _team(client, admin)
    member = await _member_token(client, admin, team, "m3@corp.com", "member")

    off_list = await client.post(
        f"/teams/{team}/mcp-server-proposals",
        json={"name": "evil", "url": "https://localhost:9999/mcp"},
        headers=_bearer(member),
    )

    assert off_list.status_code == HTTP_400_BAD_REQUEST
    assert "MCP_ALLOWED_HOSTS" in off_list.text
    assert discoveries.urls == []
