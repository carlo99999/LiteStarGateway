"""Global (platform) models and extending a team model to other teams.

Covers the resolution rules end to end through the HTTP surface: a global model
is callable by any team; an extended model is callable by the target team under
its (possibly disambiguated) alias; and the priority own > extended > global
holds, with `-<team>` / `-global` suffixes on a clash.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_401_UNAUTHORIZED,
    HTTP_409_CONFLICT,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

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


async def _admin(client: AsyncTestClient) -> str:
    resp = await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    return resp.json()["access_token"]


async def _credential(client: AsyncTestClient, admin: str) -> str:
    resp = await client.post(
        "/credentials",
        json={"name": "c-openai", "provider": "openai", "values": {"api_key": "x"}},
        headers=_bearer(admin),
    )
    return resp.json()["id"]


async def _team(client: AsyncTestClient, admin: str, org: str, name: str) -> str:
    resp = await client.post(
        f"/organizations/{org}/teams",
        json={"name": name, "admin_email": ADMIN_EMAIL},
        headers=_bearer(admin),
    )
    return resp.json()["id"]


async def _team_model(client: AsyncTestClient, admin: str, team: str, cred: str, name: str) -> str:
    resp = await client.post(
        f"/teams/{team}/models",
        json={
            "name": name,
            "provider": "openai",
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "gpt-4o",
        },
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_201_CREATED, resp.text
    return resp.json()["id"]


async def _global_model(client: AsyncTestClient, admin: str, cred: str, name: str) -> str:
    resp = await client.post(
        "/platform/models",
        json={
            "name": name,
            "provider": "openai",
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "gpt-4o",
        },
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_201_CREATED, resp.text
    return resp.json()["id"]


async def _team_key(client: AsyncTestClient, admin: str, team: str) -> str:
    resp = await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    return resp.json()["plaintext"]


async def _catalog(client: AsyncTestClient, key: str) -> set[str]:
    """The set of model ids a team can call, from GET /v1/models."""
    resp = await client.get("/v1/models", headers=_bearer(key))
    assert resp.status_code == HTTP_200_OK, resp.text
    return {entry["id"] for entry in resp.json()["data"]}


async def _org(client: AsyncTestClient, admin: str) -> str:
    resp = await client.post("/organizations", json={"name": "Acme"}, headers=_bearer(admin))
    return resp.json()["id"]


async def test_global_model_is_callable_by_a_team(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    org = await _org(client, admin)
    team = await _team(client, admin, org, "Core")
    await _global_model(client, admin, cred, "shared-gpt")
    key = await _team_key(client, admin, team)

    # The team never created "shared-gpt" but can call it — it's global.
    assert "shared-gpt" in await _catalog(client, key)


async def test_extend_to_team_without_collision_uses_plain_name(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    org = await _org(client, admin)
    core = await _team(client, admin, org, "Core")
    blue = await _team(client, admin, org, "Blue")
    model_id = await _team_model(client, admin, core, cred, "gpt-4o")

    resp = await client.post(
        f"/platform/models/{model_id}/extend",
        json={"team_ids": [blue]},
        headers=_bearer(admin),
    )
    assert resp.status_code in (HTTP_200_OK, HTTP_201_CREATED), resp.text
    grants = resp.json()
    assert len(grants) == 1
    assert grants[0]["alias"] == "gpt-4o"

    blue_key = await _team_key(client, admin, blue)
    assert "gpt-4o" in await _catalog(client, blue_key)


async def test_extend_collision_suffixes_with_source_team(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    org = await _org(client, admin)
    core = await _team(client, admin, org, "Core")
    blue = await _team(client, admin, org, "Blue")
    # Blue already has its own "gpt-4o".
    await _team_model(client, admin, blue, cred, "gpt-4o")
    core_model = await _team_model(client, admin, core, cred, "gpt-4o")

    resp = await client.post(
        f"/platform/models/{core_model}/extend",
        json={"team_ids": [blue]},
        headers=_bearer(admin),
    )
    assert resp.json()[0]["alias"] == "gpt-4o-core"

    blue_key = await _team_key(client, admin, blue)
    catalog = await _catalog(client, blue_key)
    assert "gpt-4o" in catalog  # Blue's own
    assert "gpt-4o-core" in catalog  # the extended one


async def test_delete_team_model_with_active_grant_is_rejected(client: AsyncTestClient) -> None:
    """ISSUE-020: deleting a source model with live extension grants must not
    silently cascade-revoke other teams' access, mirroring the RouterShared
    guard on the router side."""
    admin = await _admin(client)
    cred = await _credential(client, admin)
    org = await _org(client, admin)
    core = await _team(client, admin, org, "Core")
    blue = await _team(client, admin, org, "Blue")
    model_id = await _team_model(client, admin, core, cred, "gpt-4o")
    await client.post(
        f"/platform/models/{model_id}/extend",
        json={"team_ids": [blue]},
        headers=_bearer(admin),
    )

    resp = await client.delete(f"/teams/{core}/models/{model_id}", headers=_bearer(admin))
    assert resp.status_code == HTTP_409_CONFLICT, resp.text

    blue_key = await _team_key(client, admin, blue)
    assert "gpt-4o" in await _catalog(client, blue_key)  # grant survives the rejected delete


async def test_delete_team_model_after_revoking_grants_succeeds(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    org = await _org(client, admin)
    core = await _team(client, admin, org, "Core")
    blue = await _team(client, admin, org, "Blue")
    model_id = await _team_model(client, admin, core, cred, "gpt-4o")
    grant_id = (
        await client.post(
            f"/platform/models/{model_id}/extend",
            json={"team_ids": [blue]},
            headers=_bearer(admin),
        )
    ).json()[0]["id"]

    await client.delete(f"/platform/models/grants/{grant_id}", headers=_bearer(admin))

    resp = await client.delete(f"/teams/{core}/models/{model_id}", headers=_bearer(admin))
    assert resp.status_code == HTTP_204_NO_CONTENT, resp.text


async def test_global_collision_exposes_dash_global(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    org = await _org(client, admin)
    team = await _team(client, admin, org, "Core")
    await _global_model(client, admin, cred, "gpt-4o")
    await _team_model(client, admin, team, cred, "gpt-4o")  # team shadows the global
    key = await _team_key(client, admin, team)

    catalog = await _catalog(client, key)
    assert "gpt-4o" in catalog  # the team's own wins the plain name
    assert "gpt-4o-global" in catalog  # the global is still reachable, disambiguated


async def test_unextend_removes_it_from_the_target(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    org = await _org(client, admin)
    core = await _team(client, admin, org, "Core")
    blue = await _team(client, admin, org, "Blue")
    model_id = await _team_model(client, admin, core, cred, "gpt-4o")
    grant_id = (
        await client.post(
            f"/platform/models/{model_id}/extend",
            json={"team_ids": [blue]},
            headers=_bearer(admin),
        )
    ).json()[0]["id"]

    blue_key = await _team_key(client, admin, blue)
    assert "gpt-4o" in await _catalog(client, blue_key)

    resp = await client.delete(f"/platform/models/grants/{grant_id}", headers=_bearer(admin))
    assert resp.status_code in (HTTP_200_OK, 204)
    assert "gpt-4o" not in await _catalog(client, blue_key)


async def test_callable_view_shows_own_extended_and_global(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    org = await _org(client, admin)
    core = await _team(client, admin, org, "Core")
    blue = await _team(client, admin, org, "Blue")
    await _global_model(client, admin, cred, "g")
    await _team_model(client, admin, core, cred, "own-m")
    blue_model = await _team_model(client, admin, blue, cred, "from-blue")
    await client.post(
        f"/platform/models/{blue_model}/extend",
        json={"team_ids": [core]},
        headers=_bearer(admin),
    )

    resp = await client.get(f"/teams/{core}/models/callable", headers=_bearer(admin))
    assert resp.status_code == HTTP_200_OK, resp.text
    by_alias = {row["alias"]: row for row in resp.json()}

    assert by_alias["own-m"]["origin"] == "own"
    assert by_alias["g"]["origin"] == "global"
    assert by_alias["from-blue"]["origin"] == "extended"
    assert by_alias["from-blue"]["source_team_id"] == blue


async def test_make_global_promotes_and_keeps_provenance(client: AsyncTestClient) -> None:
    admin = await _admin(client)
    cred = await _credential(client, admin)
    org = await _org(client, admin)
    core = await _team(client, admin, org, "Core")
    blue = await _team(client, admin, org, "Blue")
    model_id = await _team_model(client, admin, core, cred, "promoteme")

    resp = await client.post(f"/platform/models/{model_id}/make-global", headers=_bearer(admin))
    assert resp.status_code in (HTTP_200_OK, HTTP_201_CREATED), resp.text
    body = resp.json()
    # It really became global (regression: update() used to drop team_id)…
    assert body["team_id"] is None
    # …and remembers where it came from.
    assert body["origin_team_id"] == core

    # Another team now sees it as a global model, attributed to Core.
    callable_rows = (
        await client.get(f"/teams/{blue}/models/callable", headers=_bearer(admin))
    ).json()
    promoted = next(r for r in callable_rows if r["alias"] == "promoteme")
    assert promoted["origin"] == "global"
    assert promoted["source_team_id"] == core


async def test_global_model_endpoints_reject_team_owned_id(client: AsyncTestClient) -> None:
    from litestar.status_codes import HTTP_404_NOT_FOUND

    admin = await _admin(client)
    cred = await _credential(client, admin)
    org = await _org(client, admin)
    team = await _team(client, admin, org, "Core")
    model_id = await _team_model(client, admin, team, cred, "team-only")

    updated = await client.patch(
        f"/platform/models/{model_id}",
        json={"provider_model_id": "must-not-apply"},
        headers=_bearer(admin),
    )
    deleted = await client.delete(f"/platform/models/{model_id}", headers=_bearer(admin))

    assert updated.status_code == HTTP_404_NOT_FOUND
    assert deleted.status_code == HTTP_404_NOT_FOUND
    team_models = (await client.get(f"/teams/{team}/models", headers=_bearer(admin))).json()
    assert (
        next(model for model in team_models if model["id"] == model_id)["provider_model_id"]
        == "gpt-4o"
    )
    audit_events = (await client.get("/audit", headers=_bearer(admin))).json()
    assert not any(
        event["target_id"] == model_id
        and event["action"] in {"model.update_global", "model.delete_global"}
        for event in audit_events
    )


async def test_failed_promotion_preserves_existing_grant(
    client: AsyncTestClient, database_url: str
) -> None:
    """A late global-name conflict must roll back grant removal and promotion."""
    from uuid import UUID, uuid4

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from litestar_gateway.application.model_service import ModelService
    from litestar_gateway.domain.entities import Model
    from litestar_gateway.domain.exceptions import ModelNameExists
    from litestar_gateway.infrastructure.persistence.credential_repository import (
        SQLAlchemyCredentialRepository,
    )
    from litestar_gateway.infrastructure.persistence.model_repository import (
        SQLAlchemyModelRepository,
    )
    from litestar_gateway.infrastructure.persistence.orm import ModelGrantRecord, ModelRecord

    class PromotionRaceRepository(SQLAlchemyModelRepository):
        """Stages the global row that wins after the service pre-check."""

        def _stage_name_conflict(self, model: Model) -> None:
            self._session.add(
                ModelRecord(
                    id=uuid4(),
                    team_id=None,
                    name=model.name,
                    provider=model.provider.value,
                    credential_id=model.credential_id,
                    type=model.type.value,
                    provider_model_id=model.provider_model_id,
                    params=model.params,
                    params_enforced=model.params_enforced,
                    max_output_tokens=model.max_output_tokens,
                    api_version=model.api_version,
                    input_cost_per_token=model.input_cost_per_token,
                    output_cost_per_token=model.output_cost_per_token,
                    enabled=model.enabled,
                    origin_team_id=None,
                )
            )

        async def all_global(self) -> list[Model]:
            return []

        async def update(self, model: Model) -> Model:
            self._stage_name_conflict(model)
            return await super().update(model)

        async def promote_to_global(self, model: Model) -> Model:
            self._stage_name_conflict(model)
            return await super().promote_to_global(model)

    admin = await _admin(client)
    credential_id = await _credential(client, admin)
    organization_id = await _org(client, admin)
    source_team = await _team(client, admin, organization_id, "Source")
    target_team = await _team(client, admin, organization_id, "Target")
    model_id = await _team_model(client, admin, source_team, credential_id, "race-model")
    grant_response = await client.post(
        f"/platform/models/{model_id}/extend",
        json={"team_ids": [target_team]},
        headers=_bearer(admin),
    )
    grant_id = grant_response.json()[0]["id"]

    engine = create_async_engine(database_url)
    try:
        async with AsyncSession(engine) as session:
            repository = PromotionRaceRepository(session)
            service = ModelService(repository, SQLAlchemyCredentialRepository(session))
            with pytest.raises(ModelNameExists):
                await service.make_global(UUID(model_id))

        async with AsyncSession(engine) as verification_session:
            grant = await verification_session.get(ModelGrantRecord, UUID(grant_id))
            source = await verification_session.get(ModelRecord, UUID(model_id))
            assert grant is not None
            assert source is not None
            assert source.team_id == UUID(source_team)
    finally:
        await engine.dispose()


async def test_platform_routes_require_admin(client: AsyncTestClient) -> None:
    # No token → the platform-admin guard rejects before any work.
    resp = await client.get("/platform/models")
    assert resp.status_code == HTTP_401_UNAUTHORIZED
