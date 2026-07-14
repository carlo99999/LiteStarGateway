"""Unit tests for APIKeyService using an in-memory fake repository.

Demonstrates the ports/adapters split: the service is tested with no database.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest

from litestar_gateway.application.service import APIKeyService
from litestar_gateway.domain.entities import APIKey, User
from litestar_gateway.domain.exceptions import APIKeyNotFound, InvalidAPIKey
from litestar_gateway.domain.key_generator import generate_key
from litestar_gateway.domain.ports import UserRepository

TEAM_ID = uuid4()
USER_ID = uuid4()


class FakeAPIKeyRepository:
    """In-memory implementation of the APIKeyRepository port."""

    def __init__(self) -> None:
        self._store: dict[UUID, APIKey] = {}
        self.update_calls = 0
        self.revoke_after_hash_read = False
        self.fail_after_add = False

    async def add(self, key: APIKey) -> APIKey:
        self._store[key.id] = key
        if self.fail_after_add:
            raise RuntimeError("injected add failure")
        return key

    async def get(self, key_id: UUID) -> APIKey | None:
        return self._store.get(key_id)

    async def get_for_update(self, key_id: UUID) -> APIKey | None:
        return await self.get(key_id)

    async def get_by_hash(self, key_hash: str) -> APIKey | None:
        key = next((k for k in self._store.values() if k.key_hash == key_hash), None)
        if key is not None and self.revoke_after_hash_read:
            self._store[key.id] = dataclasses.replace(key, revoked_at=datetime.now(UTC))
        return key

    async def list_by_team(
        self, team_id: UUID, *, limit: int = 100, offset: int = 0
    ) -> list[APIKey]:
        keys = [k for k in self._store.values() if k.team_id == team_id]
        return keys[offset : offset + limit]

    async def list_by_creator(
        self, created_by: UUID, *, limit: int = 100, offset: int = 0
    ) -> list[APIKey]:
        keys = [k for k in self._store.values() if k.created_by == created_by]
        return keys[offset : offset + limit]

    async def update(self, key: APIKey) -> APIKey:
        self.update_calls += 1
        self._store[key.id] = key
        return key

    async def touch_last_used(self, key_id: UUID, last_used_at: datetime) -> bool:
        key = self._store.get(key_id)
        if key is None or not key.is_active:
            return False
        self.update_calls += 1
        self._store[key_id] = dataclasses.replace(key, last_used_at=last_used_at)
        return True

    async def is_authenticatable(self, key_id: UUID) -> bool:
        key = self._store.get(key_id)
        return key is not None and key.is_active

    async def schedule_revocation(
        self,
        key_id: UUID,
        expected_revoked_at: datetime | None,
        revoked_at: datetime,
    ) -> bool:
        key = self._store.get(key_id)
        if key is None or key.revoked_at != expected_revoked_at:
            return False
        self._store[key_id] = dataclasses.replace(key, revoked_at=revoked_at)
        return True

    async def revoke_personal_keys_for_user(self, user_id: UUID, revoked_at: datetime) -> None:
        for key_id, key in list(self._store.items()):
            if (
                key.created_by == user_id
                and key.service_principal_id is None
                and (key.revoked_at is None or key.revoked_at > revoked_at)
            ):
                self._store[key_id] = dataclasses.replace(key, revoked_at=revoked_at)

    async def revoke_for_service_principal(
        self, service_principal_id: UUID, revoked_at: datetime
    ) -> None:
        for key_id, key in list(self._store.items()):
            if key.service_principal_id == service_principal_id:
                self._store[key_id] = dataclasses.replace(key, revoked_at=revoked_at)


class FakeTransaction:
    def __init__(self, repository: FakeAPIKeyRepository) -> None:
        self._repository = repository
        self._snapshot: dict[UUID, APIKey] = {}

    async def commit(self) -> None:
        self._snapshot = dict(self._repository._store)

    async def rollback(self) -> None:
        self._repository._store = dict(self._snapshot)


class FakeUserRepository:
    def __init__(self) -> None:
        self._user = User(
            id=USER_ID,
            email="user@example.com",
            password_hash="unused",  # pragma: allowlist secret
            is_admin=False,
            created_at=datetime.now(UTC),
        )

    async def get(self, user_id: UUID) -> User | None:
        return self._user if user_id == self._user.id else None

    async def get_for_update(self, user_id: UUID) -> User | None:
        return await self.get(user_id)


def _service(repository: FakeAPIKeyRepository) -> APIKeyService:
    return APIKeyService(
        repository,
        transaction=FakeTransaction(repository),
        users=cast(UserRepository, FakeUserRepository()),
    )


@pytest.fixture
def service() -> APIKeyService:
    return _service(FakeAPIKeyRepository())


async def test_last_used_write_is_throttled() -> None:
    repo = FakeAPIKeyRepository()
    service = _service(repo)
    issued = await service.issue(team_id=TEAM_ID, created_by=USER_ID)

    await service.authenticate(issued.plaintext)  # first use: last_used None -> writes
    assert repo.update_calls == 1
    await service.authenticate(issued.plaintext)  # within throttle window -> no write
    assert repo.update_calls == 1


async def test_issue_then_authenticate(service: APIKeyService) -> None:
    issued = await service.issue(team_id=TEAM_ID, created_by=USER_ID, name="ci")
    authed = await service.authenticate(issued.plaintext)
    assert authed.team_id == TEAM_ID
    assert authed.created_by == USER_ID
    assert authed.last_used_at is not None


async def test_authenticate_unknown_key_raises(service: APIKeyService) -> None:
    with pytest.raises(InvalidAPIKey):
        await service.authenticate("lsk_does_not_exist")


async def test_sp_authentication_fails_closed_without_sp_repository() -> None:
    repo = FakeAPIKeyRepository()
    transaction = FakeTransaction(repo)
    material = generate_key()
    await repo.add(
        APIKey(
            id=uuid4(),
            team_id=TEAM_ID,
            created_by=USER_ID,
            name="machine",
            prefix=material.prefix,
            key_hash=material.key_hash,
            created_at=datetime.now(UTC),
            revoked_at=None,
            last_used_at=None,
            service_principal_id=uuid4(),
        )
    )
    await transaction.commit()
    service = APIKeyService(
        repo,
        transaction=transaction,
        users=cast(UserRepository, FakeUserRepository()),
    )

    with pytest.raises(InvalidAPIKey):
        await service.authenticate(material.plaintext)


async def test_revoked_key_fails_authentication(service: APIKeyService) -> None:
    issued = await service.issue(team_id=TEAM_ID, created_by=USER_ID)
    await service.revoke_for_team(TEAM_ID, issued.key.id)
    with pytest.raises(InvalidAPIKey):
        await service.authenticate(issued.plaintext)


async def test_revoke_wrong_team_raises(service: APIKeyService) -> None:
    issued = await service.issue(team_id=TEAM_ID, created_by=USER_ID)
    with pytest.raises(APIKeyNotFound):
        await service.revoke_for_team(uuid4(), issued.key.id)


async def test_list_for_team_scopes_results(service: APIKeyService) -> None:
    other_team = uuid4()
    await service.issue(team_id=TEAM_ID, created_by=USER_ID)
    await service.issue(team_id=other_team, created_by=USER_ID)
    keys = await service.list_for_team(TEAM_ID)
    assert len(keys) == 1
    assert keys[0].team_id == TEAM_ID


async def test_authentication_touch_cannot_resurrect_concurrent_revocation() -> None:
    repo = FakeAPIKeyRepository()
    service = _service(repo)
    issued = await service.issue(team_id=TEAM_ID, created_by=USER_ID)
    repo.revoke_after_hash_read = True

    with pytest.raises(InvalidAPIKey):
        await service.authenticate(issued.plaintext)

    assert not repo._store[issued.key.id].is_active


async def test_rotation_rolls_back_replacement_when_staging_fails() -> None:
    repo = FakeAPIKeyRepository()
    service = _service(repo)
    issued = await service.issue(team_id=TEAM_ID, created_by=USER_ID)
    repo.fail_after_add = True

    with pytest.raises(RuntimeError, match="injected add failure"):
        await service.rotate_for_team(TEAM_ID, issued.key.id)

    assert set(repo._store) == {issued.key.id}
