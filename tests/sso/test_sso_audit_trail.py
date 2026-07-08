"""SSO-driven privilege changes are audited (actor_type "sso"): JIT creation,
admin grant via IdP group, and team-membership reconciliation. No-op logins
(nothing changed) must add no events — the trail records changes, not logins."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from uuid import UUID

from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import TeamGrant
from litestar_gateway.domain.entities import TeamRole

from .conftest import (
    ADMIN_GROUP,
    FakeIdP,
    _admin_token,
    _callback,
    _client,
    _create_team,
    _identity,
    _settings,
)


async def _audit_events(client: AsyncTestClient) -> list[dict]:
    headers = {"Authorization": f"Bearer {await _admin_token(client)}"}
    resp = await client.get("/audit", headers=headers)
    assert resp.status_code == 200
    return resp.json()


async def test_sso_jit_creation_and_admin_grant_are_audited(tmp_path: Path) -> None:
    identity = _identity("s-audit", "alice@corp.com", groups=(ADMIN_GROUP,))
    async with _client(tmp_path, identity) as client:
        token = (await _callback(client)).json()["access_token"]
        me = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
        user_id = me.json()["id"]
        events = await _audit_events(client)

    created = next(e for e in events if e["action"] == "sso.user.create")
    granted = next(e for e in events if e["action"] == "sso.user.grant_admin")
    for event in (created, granted):
        assert event["actor_type"] == "sso"
        assert event["actor_email"] == "sso:alice@corp.com"
        assert event["target_type"] == "user"
        assert event["target_id"] == user_id


async def test_sso_admin_upgrade_audited_only_when_granted(tmp_path: Path) -> None:
    base = _settings(tmp_path)

    async def _login(groups: tuple[str, ...]) -> list[dict]:
        identity = _identity("s-up", "bob@corp.com", groups=groups)
        app = create_app(base, identity_provider=FakeIdP(identity))
        async with AsyncTestClient(app=app) as client:
            assert (await _callback(client)).status_code == 200
            return await _audit_events(client)

    events = await _login(())  # JIT-created as plain member: no grant
    assert sum(e["action"] == "sso.user.create" for e in events) == 1
    assert sum(e["action"] == "sso.user.grant_admin" for e in events) == 0

    events = await _login((ADMIN_GROUP,))  # upgraded via the IdP admin group
    assert sum(e["action"] == "sso.user.grant_admin" for e in events) == 1

    events = await _login((ADMIN_GROUP,))  # already admin: a no-op, nothing new
    assert sum(e["action"] == "sso.user.grant_admin" for e in events) == 1
    assert sum(e["action"] == "sso.user.create" for e in events) == 1


async def test_sso_membership_reconciliation_is_audited(tmp_path: Path) -> None:
    base = _settings(tmp_path)
    async with AsyncTestClient(app=create_app(base)) as admin_client:
        team_id = await _create_team(admin_client)

    mapping: dict[str, tuple[TeamGrant, ...]] = {
        "eng": (TeamGrant(UUID(team_id), TeamRole.MEMBER),),
        "eng-leads": (TeamGrant(UUID(team_id), TeamRole.ADMIN),),
    }
    settings = dataclasses.replace(base, oidc_team_mapping=mapping)

    async def _login(groups: tuple[str, ...]) -> list[dict]:
        identity = _identity("s-team", "carol@corp.com", groups=groups)
        app = create_app(settings, identity_provider=FakeIdP(identity))
        async with AsyncTestClient(app=app) as client:
            assert (await _callback(client)).status_code == 200
            return await _audit_events(client)

    events = await _login(("eng",))  # provisioned into the team as MEMBER
    adds = [e for e in events if e["action"] == "sso.team.member.add"]
    assert len(adds) == 1
    assert adds[0]["actor_type"] == "sso"
    assert adds[0]["target_type"] == "team"
    assert adds[0]["target_id"] == team_id

    events = await _login(("eng", "eng-leads"))  # role upgraded MEMBER -> ADMIN
    assert sum(e["action"] == "sso.team.member.set_role" for e in events) == 1

    events = await _login(("eng", "eng-leads"))  # same groups: no-op, nothing new
    assert sum(e["action"] == "sso.team.member.add" for e in events) == 1
    assert sum(e["action"] == "sso.team.member.set_role" for e in events) == 1

    events = await _login(())  # dropped from all mapped groups: removed
    removed = [e for e in events if e["action"] == "sso.team.member.remove"]
    assert len(removed) == 1
    assert removed[0]["target_id"] == team_id
