"""Integration tests for encrypted provider credentials."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from _invite_helpers import seed_team_and_invite
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_403_FORBIDDEN,
    HTTP_409_CONFLICT,
    HTTP_503_SERVICE_UNAVAILABLE,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

MASTER_KEY = "master-secret"
ADMIN_EMAIL = "admin@example.com"
JWT_SECRET = "test-secret-key-0123456789-abcdefghij"
SALT_KEY = "unit-test-salt-key"


def _settings(database_url: str, *, salt_key: str | None = SALT_KEY) -> Settings:
    return Settings(
        database_url=database_url,
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        salt_key=salt_key,
    )


@pytest.fixture
async def client(database_url: str) -> AsyncIterator[AsyncTestClient]:
    async with AsyncTestClient(app=create_app(_settings(database_url))) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _admin_token(client: AsyncTestClient) -> str:
    return (
        await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    ).json()["access_token"]


async def _register_and_login(client: AsyncTestClient, admin: str, email: str) -> str:
    invite = await seed_team_and_invite(client, admin)
    await client.post(
        "/signup",
        json={"invite_token": invite, "email": email, "password": "Passw0rd!"},
    )
    return (await client.post("/login", json={"email": email, "password": "Passw0rd!"})).json()[
        "access_token"
    ]


async def test_admin_creates_credential_metadata_only(client: AsyncTestClient) -> None:
    admin = await _admin_token(client)
    resp = await client.post(
        "/credentials",
        json={
            "name": "prod-openai",
            "provider": "openai",
            "values": {"api_key": "sk-secret"},
        },
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_201_CREATED
    body = resp.json()
    assert body["name"] == "prod-openai"
    assert body["provider"] == "openai"
    # Secret values must never be returned.
    assert "values" not in body
    assert "api_key" not in str(body)


async def test_list_and_delete(client: AsyncTestClient) -> None:
    admin = await _admin_token(client)
    created = (
        await client.post(
            "/credentials",
            json={"name": "c1", "provider": "anthropic", "values": {"api_key": "x"}},
            headers=_bearer(admin),
        )
    ).json()
    listing = await client.get("/credentials", headers=_bearer(admin))
    assert listing.status_code == HTTP_200_OK
    assert [c["name"] for c in listing.json()] == ["c1"]

    deleted = await client.delete(f"/credentials/{created['id']}", headers=_bearer(admin))
    assert deleted.status_code in (200, 204)
    assert (await client.get("/credentials", headers=_bearer(admin))).json() == []


async def test_duplicate_name_conflicts(client: AsyncTestClient) -> None:
    admin = await _admin_token(client)
    payload = {"name": "dup", "provider": "openai", "values": {"api_key": "x"}}
    assert (
        await client.post("/credentials", json=payload, headers=_bearer(admin))
    ).status_code == HTTP_201_CREATED
    assert (
        await client.post("/credentials", json=payload, headers=_bearer(admin))
    ).status_code == HTTP_409_CONFLICT


async def test_non_admin_forbidden(client: AsyncTestClient) -> None:
    admin = await _admin_token(client)
    user = await _register_and_login(client, admin, "user@b.com")
    resp = await client.post(
        "/credentials",
        json={"name": "x", "provider": "openai", "values": {"api_key": "x"}},
        headers=_bearer(user),
    )
    assert resp.status_code == HTTP_403_FORBIDDEN


async def test_missing_salt_key_returns_503(database_url: str) -> None:
    async with AsyncTestClient(app=create_app(_settings(database_url, salt_key=None))) as client:
        admin = await _admin_token(client)
        resp = await client.post(
            "/credentials",
            json={"name": "x", "provider": "openai", "values": {"api_key": "x"}},
            headers=_bearer(admin),
        )
        assert resp.status_code == HTTP_503_SERVICE_UNAVAILABLE


async def _team(client: AsyncTestClient, admin: str) -> str:
    org_id = (
        await client.post("/organizations", json={"name": "Acme"}, headers=_bearer(admin))
    ).json()["id"]
    return (
        await client.post(
            f"/organizations/{org_id}/teams",
            json={"name": "Core", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]


async def test_delete_in_use_credential_conflicts(client: AsyncTestClient) -> None:
    # A credential referenced by a model must not be deletable: doing so would
    # orphan the model. The guard surfaces a 409 rather than a 500 (Postgres) or
    # a silent dangling FK (SQLite).
    admin = await _admin_token(client)
    cred = (
        await client.post(
            "/credentials",
            json={"name": "in-use", "provider": "openai", "values": {"api_key": "x"}},
            headers=_bearer(admin),
        )
    ).json()
    team = await _team(client, admin)
    model = await client.post(
        f"/teams/{team}/models",
        json={
            "name": "chat",
            "provider": "openai",
            "credential_id": cred["id"],
            "type": "chat",
            "provider_model_id": "gpt-4o",
        },
        headers=_bearer(admin),
    )
    assert model.status_code == HTTP_201_CREATED

    resp = await client.delete(f"/credentials/{cred['id']}", headers=_bearer(admin))
    assert resp.status_code == HTTP_409_CONFLICT
    # The credential must survive the rejected delete.
    listing = await client.get("/credentials", headers=_bearer(admin))
    assert [c["name"] for c in listing.json()] == ["in-use"]


async def test_delete_unreferenced_credential_succeeds(client: AsyncTestClient) -> None:
    admin = await _admin_token(client)
    cred = (
        await client.post(
            "/credentials",
            json={"name": "free", "provider": "openai", "values": {"api_key": "x"}},
            headers=_bearer(admin),
        )
    ).json()
    resp = await client.delete(f"/credentials/{cred['id']}", headers=_bearer(admin))
    assert resp.status_code in (200, 204)
    assert (await client.get("/credentials", headers=_bearer(admin))).json() == []


async def test_sqlite_engine_enforces_foreign_keys(tmp_path: Path) -> None:
    # Dev/test parity with Postgres: the connect-time PRAGMA must make aiosqlite
    # enforce FKs, so an orphaning delete raises IntegrityError at the DB boundary
    # instead of silently succeeding and leaving a dangling model.credential_id.
    from uuid import uuid4

    from sqlalchemy import delete
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession

    from litestar_gateway.infrastructure.persistence.database import create_database
    from litestar_gateway.infrastructure.persistence.orm import (
        CredentialModel,
        SecretKeyModel,
        base,
    )

    # SQLite-specific parity check: force a SQLite URL even when the Postgres CI
    # job sets DATABASE_URL — this test asserts the aiosqlite FK-enforcement PRAGMA.
    settings = _settings(f"sqlite+aiosqlite:///{tmp_path / 'fk.db'}")
    engine = create_database(settings).config.get_engine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(base.UUIDAuditBase.metadata.create_all)

        # A credential references its encryption key via credential.key_id ->
        # secret_key.id. Deleting the still-referenced key must be rejected.
        key_id = uuid4()
        async with AsyncSession(engine) as session:
            session.add(SecretKeyModel(id=key_id, purpose="credential", material="m"))
            await session.flush()
            session.add(
                CredentialModel(
                    id=uuid4(),
                    name="c",
                    provider="openai",
                    encrypted_values="x",
                    key_id=key_id,
                )
            )
            await session.commit()

        async with AsyncSession(engine) as session:
            with pytest.raises(IntegrityError):
                await session.execute(delete(SecretKeyModel).where(SecretKeyModel.id == key_id))
                await session.commit()
    finally:
        await engine.dispose()


async def test_values_are_encrypted_at_rest_and_round_trip(database_url: str) -> None:
    # Stored ciphertext must not contain the secret; decryption restores it.
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from litestar_gateway.infrastructure.crypto import DataCipher, MasterCipher
    from litestar_gateway.infrastructure.persistence.orm import CredentialModel, SecretKeyModel

    settings = _settings(database_url)
    async with AsyncTestClient(app=create_app(settings)) as client:
        admin = await _admin_token(client)
        await client.post(
            "/credentials",
            json={
                "name": "c",
                "provider": "openai",
                "values": {"api_key": "super-secret-value"},
            },
            headers=_bearer(admin),
        )

    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            cred = (await conn.execute(select(CredentialModel.__table__))).one()._mapping
            key = (
                (
                    await conn.execute(
                        select(SecretKeyModel.__table__).where(
                            SecretKeyModel.__table__.c.id == cred["key_id"]
                        )
                    )
                )
                .one()
                ._mapping
            )
    finally:
        await engine.dispose()

    assert "super-secret-value" not in cred["encrypted_values"]
    # Envelope: the ciphertext is under a data key wrapped by the master (SALT_KEY).
    data_key = MasterCipher(SALT_KEY).unwrap(key["material"])
    assert DataCipher(data_key).decrypt(cred["encrypted_values"]) == {
        "api_key": "super-secret-value"
    }
