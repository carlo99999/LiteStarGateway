"""SSO_TEAM_MAPPING: IdP group → team/role reconciliation (`_resolve_team_grants`)."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from uuid import UUID, uuid4

from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import TeamGrant
from litestar_gateway.domain.entities import TeamRole
from litestar_gateway.infrastructure.web.session.sso import _resolve_team_grants

from .conftest import FakeIdP, _callback, _create_team, _identity, _settings, _team_membership


def test_resolve_team_grants_admin_wins_and_governed_is_codomain() -> None:
    t1, t2 = uuid4(), uuid4()
    mapping: dict[str, tuple[TeamGrant, ...]] = {
        "eng": (TeamGrant(t1, TeamRole.MEMBER),),
        "eng-leads": (TeamGrant(t1, TeamRole.ADMIN), TeamGrant(t2, TeamRole.MEMBER)),
    }
    desired, governed = _resolve_team_grants(("eng", "eng-leads"), mapping)
    # ADMIN wins for t1 even though "eng" also grants it as MEMBER.
    assert desired == {t1: TeamRole.ADMIN, t2: TeamRole.MEMBER}
    assert governed == {t1, t2}


def test_resolve_team_grants_governed_covers_unmatched_groups() -> None:
    # A user in none of the mapped groups still yields the full governed set, so
    # reconciliation can strip stale memberships from governed teams.
    t1 = uuid4()
    desired, governed = _resolve_team_grants(
        ("unmapped",), {"eng": (TeamGrant(t1, TeamRole.MEMBER),)}
    )
    assert desired == {}
    assert governed == {t1}


async def _me_id(client: AsyncTestClient, token: str) -> str:
    return (await client.get("/me", headers={"Authorization": f"Bearer {token}"})).json()["id"]


async def test_sso_group_mapping_provisions_team_membership(tmp_path: Path) -> None:
    # End-to-end: a bootstrap admin creates a team; SSO_TEAM_MAPPING maps an IdP
    # group to it; an SSO user in that group is auto-provisioned as a team admin.
    base = _settings(tmp_path)
    async with AsyncTestClient(app=create_app(base)) as admin_client:
        team_id = await _create_team(admin_client)

    mapping: dict[str, tuple[TeamGrant, ...]] = {
        "engineering": (TeamGrant(UUID(team_id), TeamRole.ADMIN),)
    }
    settings = dataclasses.replace(base, oidc_team_mapping=mapping)
    identity = _identity("s-map", "grace@corp.com", groups=("engineering",))
    async with AsyncTestClient(
        app=create_app(settings, identity_provider=FakeIdP(identity))
    ) as client:
        grace_id = await _me_id(client, (await _callback(client)).json()["access_token"])
        membership = await _team_membership(client, team_id, grace_id)

    assert membership is not None
    assert membership["role"] == "admin"


async def test_sso_relogin_without_group_removes_team_membership(tmp_path: Path) -> None:
    # Deprovisioning: once the user drops out of the mapped group, the next login
    # reconciles the governed team and removes the membership (IdP is authoritative).
    base = _settings(tmp_path)
    async with AsyncTestClient(app=create_app(base)) as admin_client:
        team_id = await _create_team(admin_client)

    # Map the group to MEMBER so the SSO user is never the team's (sole) admin —
    # otherwise the last-admin guard would (correctly) keep the membership.
    mapping: dict[str, tuple[TeamGrant, ...]] = {
        "engineering": (TeamGrant(UUID(team_id), TeamRole.MEMBER),)
    }
    settings = dataclasses.replace(base, oidc_team_mapping=mapping)

    async def _membership_with(groups: tuple[str, ...]) -> dict | None:
        identity = _identity("s-drop", "heidi@corp.com", groups=groups)
        async with AsyncTestClient(
            app=create_app(settings, identity_provider=FakeIdP(identity))
        ) as client:
            user_id = await _me_id(client, (await _callback(client)).json()["access_token"])
            return await _team_membership(client, team_id, user_id)

    assert await _membership_with(("engineering",)) is not None  # provisioned in
    assert await _membership_with(()) is None  # then removed when the group is gone
