"""Integration tests for encrypted provider credentials."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
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


def _settings(tmp_path: Path, *, salt_key: str | None = SALT_KEY) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'cred.db'}",
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        salt_key=salt_key,
    )


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[AsyncTestClient]:
    async with AsyncTestClient(app=create_app(_settings(tmp_path))) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _admin_token(client: AsyncTestClient) -> str:
    return (
        await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    ).json()["access_token"]


async def _register_and_login(client: AsyncTestClient, admin: str, email: str) -> str:
    invite = (await client.post("/invites", headers=_bearer(admin))).json()["token"]
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


async def test_missing_salt_key_returns_503(tmp_path: Path) -> None:
    async with AsyncTestClient(app=create_app(_settings(tmp_path, salt_key=None))) as client:
        admin = await _admin_token(client)
        resp = await client.post(
            "/credentials",
            json={"name": "x", "provider": "openai", "values": {"api_key": "x"}},
            headers=_bearer(admin),
        )
        assert resp.status_code == HTTP_503_SERVICE_UNAVAILABLE


async def test_values_are_encrypted_at_rest_and_round_trip(tmp_path: Path) -> None:
    # Stored ciphertext must not contain the secret; decryption restores it.
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from litestar_gateway.infrastructure.crypto import DataCipher, MasterCipher
    from litestar_gateway.infrastructure.persistence.orm import CredentialModel, SecretKeyModel

    settings = _settings(tmp_path)
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
