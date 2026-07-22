"""Unified callable-alias namespace regressions (R11 ISSUE-012)."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from litestar.status_codes import HTTP_201_CREATED, HTTP_409_CONFLICT
from litestar.testing import AsyncTestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import litestar_gateway.infrastructure.persistence.model_repository as model_repository_module
from litestar_gateway.app import create_app
from litestar_gateway.config import Settings
from litestar_gateway.domain.entities import Model, ModelGrant
from litestar_gateway.domain.exceptions import ModelNameExists, RouterNameExists
from litestar_gateway.domain.routing import CandidateModel, QualityTier, RouterConfig
from litestar_gateway.infrastructure.persistence.model_repository import SQLAlchemyModelRepository
from litestar_gateway.infrastructure.persistence.orm import (
    CallableAliasRecord,
    ModelGrantRecord,
)
from litestar_gateway.infrastructure.persistence.router_repository import SQLAlchemyRouterRepository

MASTER_KEY = "master-secret"
ADMIN_EMAIL = "admin@example.com"
JWT_SECRET = "test-secret-key-0123456789-abcdefghij"
SALT_KEY = "unit-test-salt-key"


@pytest.fixture
async def client(database_url: str) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=database_url,
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        salt_key=SALT_KEY,
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _setup(client: AsyncTestClient) -> tuple[str, str, str, str]:
    admin = (
        await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    ).json()["access_token"]
    credential = (
        await client.post(
            "/credentials",
            json={"name": "openai", "provider": "openai", "values": {"api_key": "x"}},
            headers=_bearer(admin),
        )
    ).json()["id"]
    organization = (
        await client.post("/organizations", json={"name": "Acme"}, headers=_bearer(admin))
    ).json()["id"]
    team = (
        await client.post(
            f"/organizations/{organization}/teams",
            json={"name": "Core", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    return admin, credential, organization, team


def _model_payload(credential: str, name: str) -> dict[str, str]:
    return {
        "name": name,
        "provider": "openai",
        "credential_id": credential,
        "type": "chat",
        "provider_model_id": "gpt-4o",
    }


def _router_payload(name: str, candidate: str) -> dict[str, object]:
    return {
        "name": name,
        "candidates": [
            {"model_name": candidate, "quality_tier": "SIMPLE", "description": "general"}
        ],
        "default_model": candidate,
        "strategy": "complexity",
        "strategy_config": {},
    }


async def _team_model(
    client: AsyncTestClient, admin: str, team: str, credential: str, name: str
) -> str:
    response = await client.post(
        f"/teams/{team}/models",
        json=_model_payload(credential, name),
        headers=_bearer(admin),
    )
    assert response.status_code == HTTP_201_CREATED, response.text
    return response.json()["id"]


@pytest.mark.parametrize("first_kind", ["model", "router"])
async def test_local_model_router_collision_is_bidirectional(
    client: AsyncTestClient, first_kind: str
) -> None:
    admin, credential, _, team = await _setup(client)
    await _team_model(client, admin, team, credential, "candidate")
    if first_kind == "model":
        await _team_model(client, admin, team, credential, "shared")
        response = await client.post(
            f"/teams/{team}/routers",
            json=_router_payload("shared", "candidate"),
            headers=_bearer(admin),
        )
    else:
        created = await client.post(
            f"/teams/{team}/routers",
            json=_router_payload("shared", "candidate"),
            headers=_bearer(admin),
        )
        assert created.status_code == HTTP_201_CREATED, created.text
        response = await client.post(
            f"/teams/{team}/models",
            json=_model_payload(credential, "shared"),
            headers=_bearer(admin),
        )

    assert response.status_code == HTTP_409_CONFLICT, response.text


@pytest.mark.parametrize("first_kind", ["model", "router"])
async def test_global_model_router_collision_is_bidirectional(
    client: AsyncTestClient, first_kind: str
) -> None:
    admin, credential, _, _ = await _setup(client)
    model = await client.post(
        "/platform/models",
        json=_model_payload(credential, "candidate"),
        headers=_bearer(admin),
    )
    assert model.status_code == HTTP_201_CREATED, model.text

    first_path = f"/platform/{first_kind}s"
    second_kind = "router" if first_kind == "model" else "model"
    second_path = f"/platform/{second_kind}s"
    first_payload = (
        _model_payload(credential, "shared")
        if first_kind == "model"
        else _router_payload("shared", "candidate")
    )
    second_payload = (
        _model_payload(credential, "shared")
        if second_kind == "model"
        else _router_payload("shared", "candidate")
    )
    created = await client.post(first_path, json=first_payload, headers=_bearer(admin))
    assert created.status_code == HTTP_201_CREATED, created.text

    response = await client.post(second_path, json=second_payload, headers=_bearer(admin))
    assert response.status_code == HTTP_409_CONFLICT, response.text


async def test_grant_disambiguates_against_other_callable_kind(
    client: AsyncTestClient,
) -> None:
    admin, credential, organization, source = await _setup(client)
    target = (
        await client.post(
            f"/organizations/{organization}/teams",
            json={"name": "Target", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    await _team_model(client, admin, target, credential, "candidate")
    router = await client.post(
        f"/teams/{target}/routers",
        json=_router_payload("shared", "candidate"),
        headers=_bearer(admin),
    )
    assert router.status_code == HTTP_201_CREATED, router.text
    source_model = await _team_model(client, admin, source, credential, "shared")

    response = await client.post(
        f"/platform/models/{source_model}/extend",
        json={"team_ids": [target]},
        headers=_bearer(admin),
    )

    assert response.status_code == HTTP_201_CREATED, response.text
    assert response.json()[0]["alias"] == "shared-core"


async def test_catalog_has_unique_typed_stable_alias_entries(client: AsyncTestClient) -> None:
    admin, credential, _, team = await _setup(client)
    model_id = await _team_model(client, admin, team, credential, "candidate")
    router = await client.post(
        f"/teams/{team}/routers",
        json=_router_payload("auto", "candidate"),
        headers=_bearer(admin),
    )
    assert router.status_code == HTTP_201_CREATED, router.text
    key = (
        await client.post(
            f"/teams/{team}/keys",
            json={"name": "key"},
            headers=_bearer(admin),
        )
    ).json()["plaintext"]

    entries = (await client.get("/v1/models", headers=_bearer(key))).json()["data"]
    aliases = [entry["id"] for entry in entries]

    assert len(aliases) == len(set(aliases))
    assert {entry["type"] for entry in entries} == {"model", "router"}
    assert next(entry for entry in entries if entry["id"] == "candidate")["resource_id"] == model_id
    router_entry = next(entry for entry in entries if entry["id"] == "auto")
    assert router_entry["resource_id"] == router.json()["id"]


async def test_occupied_synthetic_name_keeps_every_callable_reachable(
    client: AsyncTestClient,
) -> None:
    admin, credential, _, team = await _setup(client)
    global_model = await client.post(
        "/platform/models",
        json=_model_payload(credential, "shared"),
        headers=_bearer(admin),
    )
    assert global_model.status_code == HTTP_201_CREATED, global_model.text
    await _team_model(client, admin, team, credential, "shared-global")

    response = await client.post(
        f"/teams/{team}/models",
        json=_model_payload(credential, "shared"),
        headers=_bearer(admin),
    )
    assert response.status_code == HTTP_201_CREATED, response.text
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "key"}, headers=_bearer(admin))
    ).json()["plaintext"]

    aliases = {
        row["id"] for row in (await client.get("/v1/models", headers=_bearer(key))).json()["data"]
    }
    assert {"shared", "shared-global", "shared-global-2"} <= aliases


async def test_explicit_dash_global_name_has_unambiguous_nested_shadow_alias(
    client: AsyncTestClient,
) -> None:
    admin, credential, _, team = await _setup(client)
    global_model = await client.post(
        "/platform/models",
        json=_model_payload(credential, "shared-global"),
        headers=_bearer(admin),
    )
    assert global_model.status_code == HTTP_201_CREATED, global_model.text
    await _team_model(client, admin, team, credential, "shared-global")
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "key"}, headers=_bearer(admin))
    ).json()["plaintext"]

    entries = (await client.get("/v1/models", headers=_bearer(key))).json()["data"]
    aliases = {entry["id"] for entry in entries}

    assert {"shared-global", "shared-global-global"} <= aliases


async def test_synthetic_global_alias_never_displaces_declared_global_alias(
    client: AsyncTestClient,
) -> None:
    admin, credential, _, team = await _setup(client)
    global_foo = await client.post(
        "/platform/models",
        json=_model_payload(credential, "foo"),
        headers=_bearer(admin),
    )
    global_foo_suffix = await client.post(
        "/platform/models",
        json=_model_payload(credential, "foo-global"),
        headers=_bearer(admin),
    )
    assert global_foo.status_code == global_foo_suffix.status_code == HTTP_201_CREATED
    local_foo = await _team_model(client, admin, team, credential, "foo")
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "key"}, headers=_bearer(admin))
    ).json()["plaintext"]

    entries = (await client.get("/v1/models", headers=_bearer(key))).json()["data"]
    by_alias = {entry["id"]: entry["resource_id"] for entry in entries}

    assert by_alias["foo"] == local_foo
    assert by_alias["foo-global"] == global_foo_suffix.json()["id"]
    assert by_alias["foo-global-2"] == global_foo.json()["id"]


async def test_native_protocols_do_not_treat_router_as_a_concrete_model(
    client: AsyncTestClient,
) -> None:
    admin, credential, _, team = await _setup(client)
    await _team_model(client, admin, team, credential, "candidate")
    router = await client.post(
        f"/teams/{team}/routers",
        json=_router_payload("auto", "candidate"),
        headers=_bearer(admin),
    )
    assert router.status_code == HTTP_201_CREATED, router.text
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "key"}, headers=_bearer(admin))
    ).json()["plaintext"]
    body = {"model": "auto", "max_tokens": 32, "messages": [{"role": "user", "content": "x"}]}

    anthropic = await client.post("/v1/messages", json=body, headers=_bearer(key))
    gemini = await client.post(
        "/v1beta/models/auto:generateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "x"}]}]},
        headers=_bearer(key),
    )

    assert anthropic.status_code == 404
    assert gemini.status_code == 404
    assert anthropic.json()["error"]["code"] == "ModelNotFound"
    assert gemini.json()["error"]["code"] == "ModelNotFound"


async def test_concurrent_model_router_create_has_exactly_one_winner(
    client: AsyncTestClient,
) -> None:
    admin, credential, _, team = await _setup(client)
    await _team_model(client, admin, team, credential, "candidate")

    model_response, router_response = await asyncio.gather(
        client.post(
            f"/teams/{team}/models",
            json=_model_payload(credential, "contended"),
            headers=_bearer(admin),
        ),
        client.post(
            f"/teams/{team}/routers",
            json=_router_payload("contended", "candidate"),
            headers=_bearer(admin),
        ),
    )

    assert sorted([model_response.status_code, router_response.status_code]) == [201, 409]


async def test_multi_target_extend_rolls_back_every_grant_on_late_conflict(
    client: AsyncTestClient,
) -> None:
    admin, credential, organization, source = await _setup(client)
    targets = []
    for name in ("First", "Second"):
        targets.append(
            (
                await client.post(
                    f"/organizations/{organization}/teams",
                    json={"name": name, "admin_email": ADMIN_EMAIL},
                    headers=_bearer(admin),
                )
            ).json()["id"]
        )
    model_id = await _team_model(client, admin, source, credential, "shared")

    response = await client.post(
        f"/platform/models/{model_id}/extend",
        json={"team_ids": [targets[0], targets[1], targets[1]]},
        headers=_bearer(admin),
    )

    assert response.status_code == HTTP_409_CONFLICT, response.text
    for team_id in targets:
        rows = (
            await client.get(f"/teams/{team_id}/models/callable", headers=_bearer(admin))
        ).json()
        assert all(row["alias"] != "shared" for row in rows)


async def test_delete_does_not_rebind_alias_to_shadowed_global_router(
    client: AsyncTestClient,
) -> None:
    admin, credential, _, team = await _setup(client)
    global_candidate = await client.post(
        "/platform/models",
        json=_model_payload(credential, "candidate"),
        headers=_bearer(admin),
    )
    assert global_candidate.status_code == HTTP_201_CREATED, global_candidate.text
    global_router = await client.post(
        "/platform/routers",
        json=_router_payload("shared", "candidate"),
        headers=_bearer(admin),
    )
    assert global_router.status_code == HTTP_201_CREATED, global_router.text
    local_id = await _team_model(client, admin, team, credential, "shared")
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "key"}, headers=_bearer(admin))
    ).json()["plaintext"]

    deleted = await client.delete(f"/teams/{team}/models/{local_id}", headers=_bearer(admin))
    assert deleted.status_code in (200, 204), deleted.text
    catalog = (await client.get("/v1/models", headers=_bearer(key))).json()["data"]
    assert "shared" not in {row["id"] for row in catalog}
    assert "shared-global" in {row["id"] for row in catalog}
    inference = await client.post(
        "/v1/chat/completions",
        json={"model": "shared", "messages": [{"role": "user", "content": "x"}]},
        headers=_bearer(key),
    )
    assert inference.status_code == 404


async def test_completion_rejects_disabled_router_from_alias_registry(
    client: AsyncTestClient,
) -> None:
    admin, credential, _, team = await _setup(client)
    await _team_model(client, admin, team, credential, "candidate")
    created = await client.post(
        f"/teams/{team}/routers",
        json=_router_payload("disabled-router", "candidate"),
        headers=_bearer(admin),
    )
    assert created.status_code == HTTP_201_CREATED, created.text
    disabled_payload = {**_router_payload("disabled-router", "candidate"), "enabled": False}
    disabled = await client.put(
        f"/teams/{team}/routers/{created.json()['id']}",
        json=disabled_payload,
        headers=_bearer(admin),
    )
    assert disabled.status_code == 200, disabled.text
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "key"}, headers=_bearer(admin))
    ).json()["plaintext"]

    inference = await client.post(
        "/v1/chat/completions",
        json={"model": "disabled-router", "messages": [{"role": "user", "content": "x"}]},
        headers=_bearer(key),
    )

    assert inference.status_code == 404


async def test_revoke_does_not_rebind_grant_alias_to_shadowed_global_router(
    client: AsyncTestClient,
) -> None:
    admin, credential, organization, source = await _setup(client)
    target = (
        await client.post(
            f"/organizations/{organization}/teams",
            json={"name": "Target", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    candidate = await client.post(
        "/platform/models",
        json=_model_payload(credential, "candidate"),
        headers=_bearer(admin),
    )
    assert candidate.status_code == HTTP_201_CREATED, candidate.text
    router = await client.post(
        "/platform/routers",
        json=_router_payload("shared", "candidate"),
        headers=_bearer(admin),
    )
    assert router.status_code == HTTP_201_CREATED, router.text
    source_model = await _team_model(client, admin, source, credential, "shared")
    grants = await client.post(
        f"/platform/models/{source_model}/extend",
        json={"team_ids": [target]},
        headers=_bearer(admin),
    )
    assert grants.status_code == HTTP_201_CREATED, grants.text
    grant_id = grants.json()[0]["id"]
    key = (
        await client.post(f"/teams/{target}/keys", json={"name": "key"}, headers=_bearer(admin))
    ).json()["plaintext"]

    revoked = await client.delete(f"/platform/models/grants/{grant_id}", headers=_bearer(admin))
    assert revoked.status_code in (200, 204), revoked.text
    catalog = (await client.get("/v1/models", headers=_bearer(key))).json()["data"]
    assert "shared" not in {row["id"] for row in catalog}
    assert "shared-global" in {row["id"] for row in catalog}
    inference = await client.post(
        "/v1/chat/completions",
        json={"model": "shared", "messages": [{"role": "user", "content": "x"}]},
        headers=_bearer(key),
    )
    assert inference.status_code == 404


def _router_entity(team_id: UUID | None, name: str, candidate: str) -> RouterConfig:
    return RouterConfig(
        id=uuid4(),
        team_id=team_id,
        name=name,
        candidates=(
            CandidateModel(
                model_name=candidate,
                description="general",
                quality_tier=QualityTier.SIMPLE,
            ),
        ),
        default_model=candidate,
        strategy="complexity",
        strategy_config={},
        enabled=True,
        created_at=datetime.now(UTC),
        origin_team_id=team_id,
    )


async def test_concurrent_grant_and_router_create_leave_one_binding(
    client: AsyncTestClient, database_url: str
) -> None:
    admin, credential, organization, source = await _setup(client)
    target = UUID(
        (
            await client.post(
                f"/organizations/{organization}/teams",
                json={"name": "Target", "admin_email": ADMIN_EMAIL},
                headers=_bearer(admin),
            )
        ).json()["id"]
    )
    source_model = UUID(await _team_model(client, admin, source, credential, "contended"))
    engine = create_async_engine(database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as model_session, maker() as router_session:
            model_grant = ModelGrant(
                id=uuid4(),
                model_id=source_model,
                team_id=target,
                alias="contended",
                created_at=datetime.now(UTC),
            )
            outcomes = await asyncio.gather(
                SQLAlchemyModelRepository(model_session).add_grant(model_grant),
                SQLAlchemyRouterRepository(router_session).add(
                    _router_entity(target, "contended", "unused")
                ),
                return_exceptions=True,
            )
    finally:
        await engine.dispose()

    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    loser = next(outcome for outcome in outcomes if isinstance(outcome, Exception))
    assert isinstance(loser, (ModelNameExists, RouterNameExists))


async def test_concurrent_promote_and_global_router_create_roll_back_loser(
    client: AsyncTestClient, database_url: str
) -> None:
    admin, credential, _, source = await _setup(client)
    model_id = UUID(await _team_model(client, admin, source, credential, "contended"))
    engine = create_async_engine(database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as model_session, maker() as router_session:
            model_repo = SQLAlchemyModelRepository(model_session)
            model = await model_repo.get(model_id)
            assert isinstance(model, Model)
            outcomes = await asyncio.gather(
                model_repo.promote_to_global(model),
                SQLAlchemyRouterRepository(router_session).add(
                    _router_entity(None, "contended", "unused")
                ),
                return_exceptions=True,
            )
        async with maker() as verify_session:
            persisted = await SQLAlchemyModelRepository(verify_session).get(model_id)
    finally:
        await engine.dispose()

    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    loser = next(outcome for outcome in outcomes if isinstance(outcome, Exception))
    assert isinstance(loser, (ModelNameExists, RouterNameExists))
    assert persisted is not None
    if isinstance(outcomes[0], Exception):
        assert persisted.team_id == UUID(source)
    else:
        assert persisted.team_id is None


async def test_grant_waits_for_promotion_scan_and_rechecks_source_scope(
    client: AsyncTestClient,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifecycle mutex closes scan→delete on both SQLite and Postgres."""
    admin, credential, organization, source = await _setup(client)
    target = UUID(
        (
            await client.post(
                f"/organizations/{organization}/teams",
                json={"name": "Target", "admin_email": ADMIN_EMAIL},
                headers=_bearer(admin),
            )
        ).json()["id"]
    )
    model_id = UUID(await _team_model(client, admin, source, credential, "lifecycle"))
    engine = create_async_engine(database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    scan_complete = asyncio.Event()
    release_promotion = asyncio.Event()
    original = model_repository_module.tombstone_resource_grants

    async def pause_after_scan(*args, **kwargs) -> None:
        await original(*args, **kwargs)
        scan_complete.set()
        await release_promotion.wait()

    monkeypatch.setattr(model_repository_module, "tombstone_resource_grants", pause_after_scan)
    try:
        async with maker() as promotion_session, maker() as grant_session:
            promotion_repo = SQLAlchemyModelRepository(promotion_session)
            model = await promotion_repo.get(model_id)
            assert isinstance(model, Model)
            promotion = asyncio.create_task(promotion_repo.promote_to_global(model))
            await asyncio.wait_for(scan_complete.wait(), timeout=5)
            grant = asyncio.create_task(
                SQLAlchemyModelRepository(grant_session).add_grant(
                    ModelGrant(
                        id=uuid4(),
                        model_id=model_id,
                        team_id=target,
                        alias="lifecycle-source",
                        created_at=datetime.now(UTC),
                    )
                )
            )
            await asyncio.sleep(0.1)
            assert not grant.done(), "grant bypassed the resource lifecycle lock"
            release_promotion.set()
            promoted, grant_result = await asyncio.wait_for(
                asyncio.gather(promotion, grant, return_exceptions=True), timeout=10
            )
        async with maker() as verify_session:
            grants = list(
                await verify_session.scalars(
                    select(ModelGrantRecord).where(ModelGrantRecord.model_id == model_id)
                )
            )
            aliases = list(
                await verify_session.scalars(
                    select(CallableAliasRecord).where(
                        CallableAliasRecord.alias == "lifecycle-source"
                    )
                )
            )
    finally:
        release_promotion.set()
        monkeypatch.undo()
        await engine.dispose()

    assert isinstance(promoted, Model)
    assert promoted.team_id is None
    assert isinstance(grant_result, ModelNameExists)
    assert grants == []
    assert aliases == []


async def test_delete_waits_for_rename_and_tombstones_locked_name(
    client: AsyncTestClient,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, credential, _, source = await _setup(client)
    model_id = UUID(await _team_model(client, admin, source, credential, "before"))
    engine = create_async_engine(database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    rename_started = asyncio.Event()
    release_rename = asyncio.Event()
    original = model_repository_module.rename_direct

    async def pause_before_rename(*args, **kwargs) -> None:
        rename_started.set()
        await release_rename.wait()
        await original(*args, **kwargs)

    monkeypatch.setattr(model_repository_module, "rename_direct", pause_before_rename)
    try:
        async with maker() as rename_session, maker() as delete_session:
            rename_repo = SQLAlchemyModelRepository(rename_session)
            model = await rename_repo.get(model_id)
            assert isinstance(model, Model)
            rename = asyncio.create_task(
                rename_repo.update(dataclasses.replace(model, name="after"))
            )
            await asyncio.wait_for(rename_started.wait(), timeout=5)
            deletion = asyncio.create_task(
                SQLAlchemyModelRepository(delete_session).remove(model_id)
            )
            await asyncio.sleep(0.1)
            assert not deletion.done(), "delete bypassed the resource lifecycle lock"
            release_rename.set()
            renamed, deleted = await asyncio.wait_for(
                asyncio.gather(rename, deletion, return_exceptions=True), timeout=10
            )
        async with maker() as verify_session:
            persisted = await SQLAlchemyModelRepository(verify_session).get(model_id)
            after_slot = await verify_session.scalar(
                select(CallableAliasRecord).where(CallableAliasRecord.alias == "after")
            )
    finally:
        release_rename.set()
        monkeypatch.undo()
        await engine.dispose()

    assert isinstance(renamed, Model)
    assert deleted is None
    assert persisted is None
    assert after_slot is not None and after_slot.unavailable


async def test_promote_explicitly_reclaims_global_tombstone(client: AsyncTestClient) -> None:
    admin, credential, _, team = await _setup(client)
    old = await client.post(
        "/platform/models",
        json=_model_payload(credential, "reclaimed"),
        headers=_bearer(admin),
    )
    assert old.status_code == HTTP_201_CREATED, old.text
    deleted = await client.delete(f"/platform/models/{old.json()['id']}", headers=_bearer(admin))
    assert deleted.status_code in (200, 204), deleted.text
    local_id = await _team_model(client, admin, team, credential, "reclaimed")

    promoted = await client.post(f"/platform/models/{local_id}/make-global", headers=_bearer(admin))

    assert promoted.status_code == 201, promoted.text
    assert promoted.json()["team_id"] is None


async def test_router_rename_collision_is_409_and_rolls_back(client: AsyncTestClient) -> None:
    admin, credential, _, team = await _setup(client)
    await _team_model(client, admin, team, credential, "candidate")
    first = await client.post(
        f"/teams/{team}/routers",
        json=_router_payload("first", "candidate"),
        headers=_bearer(admin),
    )
    second = await client.post(
        f"/teams/{team}/routers",
        json=_router_payload("second", "candidate"),
        headers=_bearer(admin),
    )
    assert first.status_code == second.status_code == HTTP_201_CREATED

    renamed = await client.put(
        f"/teams/{team}/routers/{first.json()['id']}",
        json=_router_payload("second", "candidate"),
        headers=_bearer(admin),
    )

    assert renamed.status_code == HTTP_409_CONFLICT, renamed.text
    rows = (await client.get(f"/teams/{team}/routers", headers=_bearer(admin))).json()
    assert {row["name"] for row in rows} == {"first", "second"}


async def test_promote_preserves_plain_grant_alias_for_same_identity(
    client: AsyncTestClient,
) -> None:
    admin, credential, organization, source = await _setup(client)
    target = (
        await client.post(
            f"/organizations/{organization}/teams",
            json={"name": "Target", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    model_id = await _team_model(client, admin, source, credential, "shared")
    grant = await client.post(
        f"/platform/models/{model_id}/extend",
        json={"team_ids": [target]},
        headers=_bearer(admin),
    )
    assert grant.status_code == HTTP_201_CREATED, grant.text
    promoted = await client.post(f"/platform/models/{model_id}/make-global", headers=_bearer(admin))
    assert promoted.status_code == HTTP_201_CREATED, promoted.text
    key = (
        await client.post(f"/teams/{target}/keys", json={"name": "key"}, headers=_bearer(admin))
    ).json()["plaintext"]

    catalog = (await client.get("/v1/models", headers=_bearer(key))).json()["data"]
    shared = next(row for row in catalog if row["id"] == "shared")
    assert shared["resource_id"] == model_id


async def test_router_promote_preserves_plain_grant_alias_for_same_identity(
    client: AsyncTestClient,
) -> None:
    admin, credential, organization, source = await _setup(client)
    target = (
        await client.post(
            f"/organizations/{organization}/teams",
            json={"name": "Target", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    await _team_model(client, admin, source, credential, "candidate")
    router = await client.post(
        f"/teams/{source}/routers",
        json=_router_payload("shared", "candidate"),
        headers=_bearer(admin),
    )
    assert router.status_code == HTTP_201_CREATED, router.text
    router_id = router.json()["id"]
    grant = await client.post(
        f"/platform/routers/{router_id}/extend",
        json={"team_ids": [target]},
        headers=_bearer(admin),
    )
    assert grant.status_code == HTTP_201_CREATED, grant.text

    promoted = await client.post(
        f"/platform/routers/{router_id}/make-global", headers=_bearer(admin)
    )

    assert promoted.status_code == HTTP_201_CREATED, promoted.text
    key = (
        await client.post(f"/teams/{target}/keys", json={"name": "key"}, headers=_bearer(admin))
    ).json()["plaintext"]
    catalog = (await client.get("/v1/models", headers=_bearer(key))).json()["data"]
    shared = next(row for row in catalog if row["id"] == "shared")
    assert shared["resource_id"] == router_id


async def test_source_team_delete_does_not_rebind_extended_router_alias(
    client: AsyncTestClient,
) -> None:
    admin, credential, organization, source = await _setup(client)
    target = (
        await client.post(
            f"/organizations/{organization}/teams",
            json={"name": "Target", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    candidate = await client.post(
        "/platform/models",
        json=_model_payload(credential, "candidate"),
        headers=_bearer(admin),
    )
    assert candidate.status_code == HTTP_201_CREATED, candidate.text
    global_router = await client.post(
        "/platform/routers",
        json=_router_payload("shared", "candidate"),
        headers=_bearer(admin),
    )
    assert global_router.status_code == HTTP_201_CREATED, global_router.text
    source_router = await client.post(
        f"/teams/{source}/routers",
        json=_router_payload("shared", "candidate"),
        headers=_bearer(admin),
    )
    assert source_router.status_code == HTTP_201_CREATED, source_router.text
    grant = await client.post(
        f"/platform/routers/{source_router.json()['id']}/extend",
        json={"team_ids": [target]},
        headers=_bearer(admin),
    )
    assert grant.status_code == HTTP_201_CREATED, grant.text
    key = (
        await client.post(f"/teams/{target}/keys", json={"name": "key"}, headers=_bearer(admin))
    ).json()["plaintext"]

    deleted = await client.delete(f"/teams/{source}", headers=_bearer(admin))

    assert deleted.status_code in (200, 204), deleted.text
    catalog = (await client.get("/v1/models", headers=_bearer(key))).json()["data"]
    aliases = {row["id"] for row in catalog}
    assert "shared" not in aliases
    assert "shared-global" in aliases
