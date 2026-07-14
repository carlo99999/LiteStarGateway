"""Postgres row-lock tests for personal API-key lifecycle invariants."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID

import pytest
from _invite_helpers import issue_invite
from litestar.testing import AsyncTestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.app import create_app
from litestar_gateway.application.service import APIKeyService
from litestar_gateway.application.user_service import UserService
from litestar_gateway.config import Settings
from litestar_gateway.domain.entities import IssuedKey, User
from litestar_gateway.domain.exceptions import APIKeyNotFound, InvalidAPIKey, PermissionDenied
from litestar_gateway.infrastructure.persistence.invite_repository import (
    SQLAlchemyInviteRepository,
)
from litestar_gateway.infrastructure.persistence.password_reset_repository import (
    SQLAlchemyPasswordResetRepository,
)
from litestar_gateway.infrastructure.persistence.repository import (
    SQLAlchemyAPIKeyRepository,
)
from litestar_gateway.infrastructure.persistence.service_principal_repository import (
    SQLAlchemyServicePrincipalRepository,
)
from litestar_gateway.infrastructure.persistence.user_repository import (
    SQLAlchemyUserRepository,
)

MASTER_KEY = "master-secret"
ADMIN_EMAIL = "admin@example.com"
DEV_EMAIL = "dev@example.com"
DEV_PASSWORD = "S3cure-pass-123"  # pragma: allowlist secret


@pytest.fixture
async def client(database_url: str) -> AsyncIterator[AsyncTestClient]:
    if not database_url.startswith("postgresql"):
        pytest.skip("row-lock semantics require Postgres")
    settings = Settings(
        database_url=database_url,
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _setup_personal_key(
    client: AsyncTestClient,
) -> tuple[str, str, str, dict[str, str]]:
    admin = (
        await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    ).json()["access_token"]
    org = (
        await client.post("/organizations", json={"name": "Acme"}, headers=_bearer(admin))
    ).json()["id"]
    team = (
        await client.post(
            f"/organizations/{org}/teams",
            json={"name": "Core", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    invite = await issue_invite(client, admin, team, role="admin")
    signup = await client.post(
        "/signup",
        json={
            "invite_token": invite,
            "email": DEV_EMAIL,
            "password": DEV_PASSWORD,
        },
    )
    dev_id = signup.json()["id"]
    dev = (await client.post("/login", json={"email": DEV_EMAIL, "password": DEV_PASSWORD})).json()[
        "access_token"
    ]
    created = await client.post(
        f"/teams/{team}/keys", json={"name": "dev-key"}, headers=_bearer(dev)
    )
    assert created.status_code == 201, created.text
    return admin, dev_id, team, created.json()


def _two_party_barrier(
    original: Callable[..., Awaitable[object]],
) -> Callable[..., Awaitable[object]]:
    arrived = 0
    ready = asyncio.Event()

    async def wrapped(*args: object, **kwargs: object) -> object:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            ready.set()
        await ready.wait()
        return await original(*args, **kwargs)

    return wrapped


def _api_key_service(session: AsyncSession) -> APIKeyService:
    return APIKeyService(
        SQLAlchemyAPIKeyRepository(session),
        transaction=session,
        users=SQLAlchemyUserRepository(session),
        service_principals=SQLAlchemyServicePrincipalRepository(session),
    )


def _user_service(session: AsyncSession) -> UserService:
    return UserService(
        users=SQLAlchemyUserRepository(session),
        invites=SQLAlchemyInviteRepository(session),
        password_resets=SQLAlchemyPasswordResetRepository(session),
        transaction=session,
        api_keys=SQLAlchemyAPIKeyRepository(session),
    )


async def test_rotate_racing_deactivation_leaves_no_live_personal_key(
    client: AsyncTestClient,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, dev_id, team, created = await _setup_personal_key(client)
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(
        SQLAlchemyUserRepository,
        "get_for_update",
        _two_party_barrier(SQLAlchemyUserRepository.get_for_update),
    )

    try:
        async with sessions() as rotate_session, sessions() as deactivate_session:
            actor = await SQLAlchemyUserRepository(deactivate_session).get_by_email(ADMIN_EMAIL)
            assert actor is not None
            rotate, deactivate = await asyncio.wait_for(
                asyncio.gather(
                    _api_key_service(rotate_session).rotate_for_team(
                        UUID(team), UUID(created["id"])
                    ),
                    _user_service(deactivate_session).set_user_active(actor, UUID(dev_id), False),
                    return_exceptions=True,
                ),
                timeout=10,
            )
    finally:
        monkeypatch.undo()
        await engine.dispose()

    assert isinstance(deactivate, User)
    assert not deactivate.is_active
    assert isinstance(rotate, IssuedKey | PermissionDenied | APIKeyNotFound)
    plaintexts = [created["plaintext"]]
    if isinstance(rotate, IssuedKey):
        plaintexts.append(rotate.plaintext)
    for plaintext in plaintexts:
        assert (await client.get("/whoami", headers=_bearer(plaintext))).status_code == 401
    keys = (await client.get(f"/teams/{team}/keys", headers=_bearer(admin))).json()
    assert all(not key["is_active"] for key in keys)


async def test_concurrent_rotations_create_one_replacement(
    client: AsyncTestClient,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, _, team, created = await _setup_personal_key(client)
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(
        SQLAlchemyUserRepository,
        "get_for_update",
        _two_party_barrier(SQLAlchemyUserRepository.get_for_update),
    )

    try:
        async with sessions() as first_session, sessions() as second_session:
            results = await asyncio.wait_for(
                asyncio.gather(
                    _api_key_service(first_session).rotate_for_team(
                        UUID(team), UUID(created["id"])
                    ),
                    _api_key_service(second_session).rotate_for_team(
                        UUID(team), UUID(created["id"])
                    ),
                    return_exceptions=True,
                ),
                timeout=10,
            )
    finally:
        monkeypatch.undo()
        await engine.dispose()

    assert sum(isinstance(result, IssuedKey) for result in results) == 1
    assert sum(isinstance(result, APIKeyNotFound) for result in results) == 1
    keys = (await client.get(f"/teams/{team}/keys", headers=_bearer(admin))).json()
    assert len(keys) == 2


async def test_auth_started_before_revoke_cannot_succeed_after_commit(
    client: AsyncTestClient,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, team, created = await _setup_personal_key(client)
    # Populate a recent last_used_at so authenticate takes the throttled path:
    # the final validation must not depend on performing a telemetry write.
    assert (await client.get("/whoami", headers=_bearer(created["plaintext"]))).status_code == 200
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    hash_read = asyncio.Event()
    allow_auth_to_continue = asyncio.Event()
    original = SQLAlchemyAPIKeyRepository.get_by_hash

    async def pause_after_hash_read(
        repository: SQLAlchemyAPIKeyRepository, key_hash: str
    ) -> object:
        key = await original(repository, key_hash)
        hash_read.set()
        await allow_auth_to_continue.wait()
        return key

    monkeypatch.setattr(SQLAlchemyAPIKeyRepository, "get_by_hash", pause_after_hash_read)
    try:
        async with sessions() as auth_session, sessions() as revoke_session:
            auth_task = asyncio.create_task(
                _api_key_service(auth_session).authenticate(created["plaintext"])
            )
            await asyncio.wait_for(hash_read.wait(), timeout=10)
            await _api_key_service(revoke_session).revoke_for_team(UUID(team), UUID(created["id"]))
            allow_auth_to_continue.set()
            result = (await asyncio.gather(auth_task, return_exceptions=True))[0]
    finally:
        allow_auth_to_continue.set()
        monkeypatch.undo()
        await engine.dispose()

    assert isinstance(result, InvalidAPIKey)


async def test_service_principal_delete_detaches_keys_before_fk_delete(
    client: AsyncTestClient,
) -> None:
    admin, _, team, _ = await _setup_personal_key(client)
    principal = await client.post(
        f"/teams/{team}/service-principals",
        json={"name": "machine"},
        headers=_bearer(admin),
    )
    assert principal.status_code == 201, principal.text
    issued = await client.post(
        f"/teams/{team}/service-principals/{principal.json()['id']}/keys",
        json={"name": "machine-key", "scope": "all"},
        headers=_bearer(admin),
    )
    assert issued.status_code == 201, issued.text

    deleted = await client.delete(
        f"/teams/{team}/service-principals/{principal.json()['id']}",
        headers=_bearer(admin),
    )

    assert deleted.status_code == 204, deleted.text
    assert (
        await client.get("/whoami", headers=_bearer(issued.json()["plaintext"]))
    ).status_code == 401
