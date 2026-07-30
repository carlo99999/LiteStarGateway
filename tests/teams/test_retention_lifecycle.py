"""Plan 13 Phase 5 — retention and deletion lifecycle.

Covers: soft-delete (tombstone) for a team with billed history vs. the
unchanged hard-delete fast path for a team without one, the export-before-delete
action, and the separate, audited, admin-only purge action.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from _invite_helpers import issue_invite
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
)
from litestar.testing import AsyncTestClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.domain.entities import BudgetWindow
from litestar_gateway.infrastructure.persistence.orm import (
    BudgetAlertStateModel,
    GuardrailRuleModel,
    ModelGrantRecord,
    ModelRecord,
    PendingBudgetAlertModel,
    RouterGrantModel,
    RouterModel,
    RoutingDecisionModel,
    TeamModel,
    UsageEventModel,
)

from .conftest import ADMIN_EMAIL, MASTER_KEY, _bearer, _team_and_credential


async def _add_member(client: AsyncTestClient, admin_token: str, team_id: str, email: str) -> str:
    """Sign up a plain (non-admin) member of `team_id` and log them in."""
    invite = await issue_invite(client, admin_token, team_id, role="member")
    signup = await client.post(
        "/signup",
        json={
            "invite_token": invite,
            "email": email,
            "password": "Passw0rd!",  # pragma: allowlist secret
        },
    )
    assert signup.status_code == HTTP_201_CREATED, signup.text
    return await _login(client, email, "Passw0rd!")  # pragma: allowlist secret


async def _login(client: AsyncTestClient, email: str, password: str) -> str:
    resp = await client.post("/login", json={"email": email, "password": password})
    assert resp.status_code == HTTP_200_OK, resp.text
    return resp.json()["access_token"]


@pytest.fixture
async def raw_session(database_url: str) -> AsyncIterator[AsyncSession]:
    """A second, independent connection to the SAME database as `client` —
    for seeding usage events and inspecting rows the HTTP API deliberately
    hides (a soft-deleted team's row, its retained usage history).

    Foreign keys are enforced here on purpose. The application's engine turns the
    pragma on (`_create_engine_with_sqlite_fk`), but this fixture builds its own,
    and aiosqlite ignores foreign keys unless asked *per connection*. Without it a
    seeded row can reference a parent that does not exist, which SQLite accepts and
    Postgres rejects — so the test passes locally and fails in CI fourteen minutes
    later. That has happened here: a row seeded with a fabricated parent id reached
    main and only the Postgres job caught it.
    """
    engine = create_async_engine(database_url)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection: object, _: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed_usage_event(session: AsyncSession, team_id: str) -> None:
    session.add(
        UsageEventModel(
            id=uuid4(),
            team_id=UUID(team_id),
            model_id=uuid4(),
            model_name="gpt-4o",
            operation="chat",
            prompt_tokens=10,
            completion_tokens=5,
            cost=0.01,
            created_at=datetime.now(UTC),
        )
    )
    await session.commit()


async def test_delete_team_without_usage_is_still_hard_deleted(
    client: AsyncTestClient, raw_session: AsyncSession
) -> None:
    """Regression: a team with NO billed history is removed exactly as
    before — no tombstone row left behind."""
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    team_id, _cred = await _team_and_credential(client, admin, "NoUsage")

    resp = await client.delete(f"/teams/{team_id}", headers=_bearer(admin))
    assert resp.status_code == HTTP_204_NO_CONTENT, resp.text

    model = await raw_session.get(TeamModel, UUID(team_id))
    assert model is None


async def test_delete_team_with_usage_history_is_soft_deleted(
    client: AsyncTestClient, raw_session: AsyncSession
) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = (
        await client.post("/organizations", json={"name": "O-tombstone"}, headers=_bearer(admin))
    ).json()["id"]
    team_id = (
        await client.post(
            f"/organizations/{org_id}/teams",
            json={"name": "Billed", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    await _seed_usage_event(raw_session, team_id)

    resp = await client.delete(f"/teams/{team_id}", headers=_bearer(admin))
    assert resp.status_code == HTTP_204_NO_CONTENT, resp.text

    # Hidden from the ordinary read/list paths...
    assert (
        await client.get(f"/teams/{team_id}", headers=_bearer(admin))
    ).status_code == HTTP_404_NOT_FOUND
    listed = (await client.get("/teams", headers=_bearer(admin))).json()
    assert team_id not in {t["id"] for t in listed}

    # ...but nothing was actually lost: the team row and its usage history are
    # both still there, tombstoned rather than gone.
    model = await raw_session.get(TeamModel, UUID(team_id))
    assert model is not None
    assert model.deleted_at is not None
    usage_rows = (
        await raw_session.scalars(
            select(UsageEventModel).where(UsageEventModel.team_id == UUID(team_id))
        )
    ).all()
    assert len(usage_rows) == 1


async def test_export_team_returns_usage_and_audit_history(
    client: AsyncTestClient, raw_session: AsyncSession
) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    team_id, _cred = await _team_and_credential(client, admin, "Export")
    await _seed_usage_event(raw_session, team_id)
    # Generates a "team.update" audit event targeting this team.
    renamed = await client.patch(
        f"/teams/{team_id}", json={"name": "Exported"}, headers=_bearer(admin)
    )
    assert renamed.status_code == HTTP_200_OK, renamed.text

    resp = await client.get(f"/teams/{team_id}/export", headers=_bearer(admin))
    assert resp.status_code == HTTP_200_OK, resp.text
    body = resp.json()
    assert body["team"]["id"] == team_id
    assert len(body["usage_events"]) == 1
    assert body["usage_events"][0]["cost"] == pytest.approx(0.01)
    assert any(e["action"] == "team.update" for e in body["audit_events"])
    assert body["routing_savings"] == {
        "total_estimated_savings": 0.0,
        "decisions_counted": 0,
        "decisions_without_usage": 0,
    }


async def test_export_team_forbidden_for_non_admin(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    team_id, _cred = await _team_and_credential(client, admin, "ExportRBAC")
    member = await _add_member(client, admin, team_id, "member@example.com")

    resp = await client.get(f"/teams/{team_id}/export", headers=_bearer(member))
    assert resp.status_code == HTTP_403_FORBIDDEN, resp.text


async def test_purge_requires_platform_admin(
    client: AsyncTestClient, raw_session: AsyncSession
) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    team_id, _cred = await _team_and_credential(client, admin, "PurgeRBAC")
    not_admin = await _add_member(client, admin, team_id, "notadmin@example.com")
    await _seed_usage_event(raw_session, team_id)
    assert (
        await client.delete(f"/teams/{team_id}", headers=_bearer(admin))
    ).status_code == HTTP_204_NO_CONTENT

    resp = await client.post(f"/teams/{team_id}/purge", headers=_bearer(not_admin))
    assert resp.status_code == HTTP_403_FORBIDDEN, resp.text


async def test_purge_requires_soft_deleted_first(client: AsyncTestClient) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    team_id, _cred = await _team_and_credential(client, admin, "PurgeLive")

    resp = await client.post(f"/teams/{team_id}/purge", headers=_bearer(admin))
    assert resp.status_code == HTTP_409_CONFLICT, resp.text


async def test_purge_removes_data_and_is_audited(
    client: AsyncTestClient, raw_session: AsyncSession
) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = (
        await client.post("/organizations", json={"name": "O-purge"}, headers=_bearer(admin))
    ).json()["id"]
    team_id = (
        await client.post(
            f"/organizations/{org_id}/teams",
            json={"name": "ToPurge", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    await _seed_usage_event(raw_session, team_id)
    assert (
        await client.delete(f"/teams/{team_id}", headers=_bearer(admin))
    ).status_code == HTTP_204_NO_CONTENT

    resp = await client.post(f"/teams/{team_id}/purge", headers=_bearer(admin))
    assert resp.status_code == HTTP_204_NO_CONTENT, resp.text

    # Genuinely gone: a direct repository query, not just the hidden API view.
    assert await raw_session.get(TeamModel, UUID(team_id)) is None
    usage_rows = (
        await raw_session.scalars(
            select(UsageEventModel).where(UsageEventModel.team_id == UUID(team_id))
        )
    ).all()
    assert usage_rows == []

    # And it left an audit trail of its own.
    audit = (await client.get("/audit", headers=_bearer(admin))).json()
    purge_events = [e for e in audit if e["action"] == "team.purge" and e["target_id"] == team_id]
    assert len(purge_events) == 1
    assert purge_events[0]["actor_email"] == ADMIN_EMAIL


async def test_purge_twice_is_not_found_second_time(
    client: AsyncTestClient, raw_session: AsyncSession
) -> None:
    """Purge is genuinely irreversible: once the team row is gone, a second
    purge attempt has nothing left to act on (404), never a silent success."""
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    team_id, _cred = await _team_and_credential(client, admin, "PurgeTwice")
    await _seed_usage_event(raw_session, team_id)
    await client.delete(f"/teams/{team_id}", headers=_bearer(admin))
    first = await client.post(f"/teams/{team_id}/purge", headers=_bearer(admin))
    assert first.status_code == HTTP_204_NO_CONTENT, first.text

    second = await client.post(f"/teams/{team_id}/purge", headers=_bearer(admin))
    assert second.status_code == HTTP_404_NOT_FOUND, second.text


# ---------------------------------------------------------------------------
# ISSUE-030: the purge must clear every team-scoped row, not just usage.
# ---------------------------------------------------------------------------


async def _seed_alert_history(session: AsyncSession, team_id: str) -> None:
    """A fired threshold plus its queued notification — the ordinary state of
    any team that has ever crossed a budget threshold."""
    period_start = datetime(2026, 7, 1, tzinfo=UTC)
    session.add(
        BudgetAlertStateModel(
            id=uuid4(),
            team_id=UUID(team_id),
            window=BudgetWindow.MONTHLY.value,
            period_start=period_start,
            threshold=80,
            fired_at=datetime.now(UTC),
        )
    )
    session.add(
        PendingBudgetAlertModel(
            id=uuid4(),
            team_id=UUID(team_id),
            window=BudgetWindow.MONTHLY.value,
            period_start=period_start,
            threshold=80,
            spend=85.0,
            limit_cost=100.0,
        )
    )
    await session.commit()


async def _seed_received_grants(
    session: AsyncSession, team_id: str, other_team_id: str, credential_id: str
) -> None:
    """Grants this team RECEIVED: the FK points at `team.id` from a row the
    team does not own, which the purge never looked at. The source model needs
    a real credential — PostgreSQL enforces that FK even when SQLite does not."""
    model_id, router_id = uuid4(), uuid4()
    session.add(
        ModelRecord(
            id=model_id,
            team_id=UUID(other_team_id),
            name=f"shared-model-{model_id.hex[:6]}",
            provider="openai",
            credential_id=UUID(credential_id),
            type="chat",
            provider_model_id="gpt-4o",
            params={},
            params_enforced={},
            enabled=True,
            image_prices={},
        )
    )
    session.add(
        RouterModel(
            id=router_id,
            team_id=UUID(other_team_id),
            name=f"shared-router-{router_id.hex[:6]}",
            candidates=[],
            default_model="gpt-4o",
            strategy="rules",
            enabled=True,
        )
    )
    await session.flush()
    session.add(
        ModelGrantRecord(
            id=uuid4(), model_id=model_id, team_id=UUID(team_id), alias="granted-model"
        )
    )
    session.add(
        RouterGrantModel(
            id=uuid4(), router_id=router_id, team_id=UUID(team_id), alias="granted-router"
        )
    )
    await session.commit()


async def _seed_routing_decision(session: AsyncSession, team_id: str) -> None:
    """A decision row: no FK, so it never blocked the delete — it just stayed
    behind, prompts and all."""
    session.add(
        RoutingDecisionModel(
            id=uuid4(),
            team_id=UUID(team_id),
            router_name="auto",
            strategy="rules",
            chosen_model="gpt-4o",
            user_text="my private prompt",
            system_prompt="my private system prompt",
        )
    )
    await session.commit()


async def _tombstoned_team(client: AsyncTestClient, admin: str, raw_session: AsyncSession) -> str:
    org_id = (
        await client.post(
            "/organizations", json={"name": f"O-{uuid4().hex[:6]}"}, headers=_bearer(admin)
        )
    ).json()["id"]
    team_id = (
        await client.post(
            f"/organizations/{org_id}/teams",
            json={"name": f"T-{uuid4().hex[:6]}", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    await _seed_usage_event(raw_session, team_id)  # gives it billed history
    assert (
        await client.delete(f"/teams/{team_id}", headers=_bearer(admin))
    ).status_code == HTTP_204_NO_CONTENT
    return team_id


async def test_a_team_with_budget_alert_history_can_still_be_purged(
    client: AsyncTestClient, raw_session: AsyncSession
) -> None:
    """The reported false denial: the alert tables carry FKs to `team.id`, so
    the delete failed and the IntegrityError surfaced as 409 TeamNotEmpty —
    a team that ever crossed a threshold was simply not purgeable."""
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    team_id = await _tombstoned_team(client, admin, raw_session)
    await _seed_alert_history(raw_session, team_id)

    resp = await client.post(f"/teams/{team_id}/purge", headers=_bearer(admin))

    assert resp.status_code == HTTP_204_NO_CONTENT, resp.text
    assert await raw_session.get(TeamModel, UUID(team_id)) is None
    for model in (BudgetAlertStateModel, PendingBudgetAlertModel):
        rows = (
            await raw_session.scalars(select(model).where(model.team_id == UUID(team_id)))
        ).all()
        assert rows == [], model.__name__


async def test_a_team_holding_received_grants_can_still_be_purged(
    client: AsyncTestClient, raw_session: AsyncSession
) -> None:
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    # The granting team stays alive: what is under test is the receiving team's
    # purge, blocked by rows another team owns.
    other_team_id, credential_id = await _team_and_credential(client, admin, "Sharer")
    team_id = await _tombstoned_team(client, admin, raw_session)
    await _seed_received_grants(raw_session, team_id, other_team_id, credential_id)

    resp = await client.post(f"/teams/{team_id}/purge", headers=_bearer(admin))

    assert resp.status_code == HTTP_204_NO_CONTENT, resp.text
    for model in (ModelGrantRecord, RouterGrantModel):
        rows = (
            await raw_session.scalars(select(model).where(model.team_id == UUID(team_id)))
        ).all()
        assert rows == [], model.__name__


async def test_purge_removes_routing_decisions_and_their_prompts(
    client: AsyncTestClient, raw_session: AsyncSession
) -> None:
    """No FK means nothing blocked the delete — and nothing deleted the row
    either. Routing decisions keep `user_text`/`system_prompt`, so an
    irreversible purge that leaves them behind is not one."""
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    team_id = await _tombstoned_team(client, admin, raw_session)
    await _seed_routing_decision(raw_session, team_id)

    resp = await client.post(f"/teams/{team_id}/purge", headers=_bearer(admin))

    assert resp.status_code == HTTP_204_NO_CONTENT, resp.text
    rows = (
        await raw_session.scalars(
            select(RoutingDecisionModel).where(RoutingDecisionModel.team_id == UUID(team_id))
        )
    ).all()
    assert rows == []


async def _seed_team_wide_guardrail_rule(session: AsyncSession, team_id: str) -> None:
    """A rule scoped to the whole team: `model_id` and `router_id` both NULL.

    Model- and router-scoped rules cascade away with their model or router, so
    only the team-wide row — the common configuration — reaches the team delete.
    """
    session.add(
        GuardrailRuleModel(
            id=uuid4(),
            team_id=UUID(team_id),
            name="pii",
            kind="judge",
            direction="request",
            fail_policy="closed",
        )
    )
    await session.commit()


async def test_a_team_with_a_team_wide_guardrail_rule_can_still_be_purged(
    client: AsyncTestClient, raw_session: AsyncSession
) -> None:
    """`guardrail_rule.team_id` is an FK with no `ondelete`, and the table was
    missing from the purge child list — so the delete raised and surfaced as
    409 TeamNotEmpty. The team was tombstoned but never purgeable, and its
    envelope-encrypted signing secret outlived an 'irreversible' purge."""
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    team_id = await _tombstoned_team(client, admin, raw_session)
    await _seed_team_wide_guardrail_rule(raw_session, team_id)

    resp = await client.post(f"/teams/{team_id}/purge", headers=_bearer(admin))

    assert resp.status_code == HTTP_204_NO_CONTENT, resp.text
    assert await raw_session.get(TeamModel, UUID(team_id)) is None
    rows = (
        await raw_session.scalars(
            select(GuardrailRuleModel).where(GuardrailRuleModel.team_id == UUID(team_id))
        )
    ).all()
    assert rows == []


async def test_a_team_with_a_guardrail_rule_and_no_usage_is_still_hard_deleted(
    client: AsyncTestClient, raw_session: AsyncSession
) -> None:
    """The same FK also broke the ordinary delete, which is the wider blast
    radius: a team with no billed history, no models and no keys still answered
    409 'team not empty' — naming a condition the operator could not find,
    because guardrail rules are not part of what the message describes."""
    admin = await _login(client, ADMIN_EMAIL, MASTER_KEY)
    org_id = (
        await client.post(
            "/organizations", json={"name": f"O-{uuid4().hex[:6]}"}, headers=_bearer(admin)
        )
    ).json()["id"]
    team_id = (
        await client.post(
            f"/organizations/{org_id}/teams",
            json={"name": f"T-{uuid4().hex[:6]}", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    await _seed_team_wide_guardrail_rule(raw_session, team_id)

    resp = await client.delete(f"/teams/{team_id}", headers=_bearer(admin))

    assert resp.status_code == HTTP_204_NO_CONTENT, resp.text
    assert await raw_session.get(TeamModel, UUID(team_id)) is None
