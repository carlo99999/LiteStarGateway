"""Deterministic lifecycle races at the SCIM service boundary."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from litestar_gateway.application.scim_service import ScimService
from litestar_gateway.domain.entities import User
from litestar_gateway.domain.ports import ScimTokenRepository, UserRepository


class FakeTransaction:
    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class AdminWinsRaceUserRepository:
    def __init__(self, user: User) -> None:
        self.user = user
        self.set_active_calls = 0

    async def get(self, user_id: UUID) -> User | None:
        return self.user if user_id == self.user.id else None

    async def get_for_update(self, user_id: UUID) -> User | None:
        if user_id != self.user.id:
            return None
        self.user = dataclasses.replace(self.user, is_active=False, deactivated_by="admin")
        return self.user

    async def set_active(
        self, user_id: UUID, is_active: bool, *, deactivated_by: str | None = None
    ) -> None:
        self.set_active_calls += 1
        self.user = dataclasses.replace(
            self.user, is_active=is_active, deactivated_by=deactivated_by
        )


async def test_admin_deactivation_wins_race_with_scim_deactivation() -> None:
    user = User(
        id=uuid4(),
        email="user@example.com",
        password_hash="unused",  # pragma: allowlist secret
        is_admin=False,
        created_at=datetime.now(UTC),
    )
    users = AdminWinsRaceUserRepository(user)
    service = ScimService(
        users=cast(UserRepository, users),
        tokens=cast(ScimTokenRepository, object()),
        transaction=FakeTransaction(),
    )

    updated = await service.update_user(user.id, active=False)

    assert not updated.is_active
    assert updated.deactivated_by == "admin"
    assert users.set_active_calls == 0
