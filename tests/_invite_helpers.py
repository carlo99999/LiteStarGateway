"""Shared HTTP helpers for integration tests that create users via invites.

Invites now require a real team (the invited user joins it on signup), so tests
seed an org + team before issuing an invite. The team admin is the bootstrap
admin, which exists without an invite — breaking the otherwise circular
team↔user dependency. The seeded org/team is isolated from any team a test sets
up itself, so it never perturbs that test's member/team assertions.

Importable as `from _invite_helpers import ...` because pytest puts the `tests/`
directory (which is not itself a package) on sys.path.
"""

from __future__ import annotations

from litestar.testing import AsyncTestClient

ADMIN_EMAIL = "admin@example.com"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def seed_team(client: AsyncTestClient, admin_token: str, *, name: str = "Invite Home") -> str:
    """Create an isolated org + team via the API and return the team id."""
    org = await client.post(
        "/organizations", json={"name": f"{name} Org"}, headers=_bearer(admin_token)
    )
    org_id = org.json()["id"]
    team = await client.post(
        f"/organizations/{org_id}/teams",
        json={"name": name, "admin_email": ADMIN_EMAIL},
        headers=_bearer(admin_token),
    )
    return team.json()["id"]


async def issue_invite(
    client: AsyncTestClient, admin_token: str, team_id: str, *, role: str = "member"
) -> str:
    """Issue an invite for `team_id` and return its one-time token."""
    resp = await client.post(
        "/invites",
        json={"team_id": team_id, "role": role},
        headers=_bearer(admin_token),
    )
    return resp.json()["token"]


async def seed_team_and_invite(
    client: AsyncTestClient, admin_token: str, *, role: str = "member"
) -> str:
    """One-shot: seed an isolated team and return an invite token for it.

    For the common case where a test just needs *some* user created and doesn't
    care which team the invite is tied to."""
    team_id = await seed_team(client, admin_token)
    return await issue_invite(client, admin_token, team_id, role=role)
