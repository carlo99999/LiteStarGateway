"""Unit tests for APIKeyService using an in-memory fake repository.

Demonstrates the ports/adapters split: the service is tested with no database.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from litestar_test.application.service import APIKeyService
from litestar_test.domain.entities import APIKey
from litestar_test.domain.exceptions import APIKeyNotFound, InvalidAPIKey

TEAM_ID = uuid4()
USER_ID = uuid4()


class FakeAPIKeyRepository:
    """In-memory implementation of the APIKeyRepository port."""

    def __init__(self) -> None:
        self._store: dict[UUID, APIKey] = {}

    async def add(self, key: APIKey) -> APIKey:
        self._store[key.id] = key
        return key

    async def get(self, key_id: UUID) -> APIKey | None:
        return self._store.get(key_id)

    async def get_by_hash(self, key_hash: str) -> APIKey | None:
        return next((k for k in self._store.values() if k.key_hash == key_hash), None)

    async def list_by_team(self, team_id: UUID) -> list[APIKey]:
        return [k for k in self._store.values() if k.team_id == team_id]

    async def update(self, key: APIKey) -> APIKey:
        self._store[key.id] = key
        return key


@pytest.fixture
def service() -> APIKeyService:
    return APIKeyService(FakeAPIKeyRepository())


async def test_issue_then_authenticate(service: APIKeyService) -> None:
    issued = await service.issue(team_id=TEAM_ID, created_by=USER_ID, name="ci")
    authed = await service.authenticate(issued.plaintext)
    assert authed.team_id == TEAM_ID
    assert authed.created_by == USER_ID
    assert authed.last_used_at is not None


async def test_authenticate_unknown_key_raises(service: APIKeyService) -> None:
    with pytest.raises(InvalidAPIKey):
        await service.authenticate("lsk_does_not_exist")


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
