"""Shared fakes/fixtures for UserService unit tests (in-memory repositories)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from litestar_gateway.application.user_service import UserService
from litestar_gateway.domain.entities import Invite, PasswordReset, User


class FakeTransaction:
    """Counts commits/rollbacks; the fakes apply writes immediately."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeUserRepository:
    def __init__(self) -> None:
        self._by_email: dict[str, User] = {}

    async def add(self, user: User) -> User:
        self._by_email[user.email] = user
        return user

    async def get_by_email(self, email: str) -> User | None:
        return self._by_email.get(email)

    async def get_by_sso_subject(self, subject: str) -> User | None:
        return next((u for u in self._by_email.values() if u.sso_subject == subject), None)

    async def bind_sso(self, user_id: UUID, sso_subject: str, is_admin: bool) -> User:
        for email, user in self._by_email.items():
            if user.id == user_id:
                updated = dataclasses.replace(user, sso_subject=sso_subject, is_admin=is_admin)
                self._by_email[email] = updated
                return updated
        raise AssertionError(f"no user {user_id}")

    async def count(self) -> int:
        return len(self._by_email)

    async def increment_token_version(self, user_id: UUID) -> None:
        for email, user in self._by_email.items():
            if user.id == user_id:
                self._by_email[email] = dataclasses.replace(
                    user, token_version=user.token_version + 1
                )

    async def set_password(self, user_id: UUID, password_hash: str) -> None:
        for email, user in self._by_email.items():
            if user.id == user_id:
                self._by_email[email] = dataclasses.replace(
                    user, password_hash=password_hash, token_version=user.token_version + 1
                )

    async def set_active(
        self, user_id: UUID, is_active: bool, *, deactivated_by: str | None = None
    ) -> None:
        for email, user in self._by_email.items():
            if user.id == user_id:
                tv = user.token_version + (0 if is_active else 1)
                self._by_email[email] = dataclasses.replace(
                    user,
                    is_active=is_active,
                    token_version=tv,
                    deactivated_by=None if is_active else deactivated_by,
                )

    async def set_admin(self, user_id: UUID, is_admin: bool) -> None:
        for email, user in self._by_email.items():
            if user.id == user_id:
                self._by_email[email] = dataclasses.replace(user, is_admin=is_admin)

    async def set_auditor(self, user_id: UUID, is_auditor: bool) -> None:
        for email, user in self._by_email.items():
            if user.id == user_id:
                self._by_email[email] = dataclasses.replace(user, is_auditor=is_auditor)

    async def get(self, user_id: UUID) -> User | None:
        return next((u for u in self._by_email.values() if u.id == user_id), None)

    async def get_for_update(self, user_id: UUID) -> User | None:
        return await self.get(user_id)

    async def register_failed_login(self, user_id: UUID) -> int:
        for email, user in self._by_email.items():
            if user.id == user_id:
                updated = dataclasses.replace(
                    user, failed_login_attempts=user.failed_login_attempts + 1
                )
                self._by_email[email] = updated
                return updated.failed_login_attempts
        return 0

    async def set_login_lock(
        self, user_id: UUID, locked_until: datetime, *, reset_cycles: bool
    ) -> None:
        for email, user in self._by_email.items():
            if user.id == user_id:
                self._by_email[email] = dataclasses.replace(
                    user,
                    locked_until=locked_until,
                    failed_login_attempts=0,
                    lockout_cycles=1 if reset_cycles else user.lockout_cycles + 1,
                )

    async def clear_login_failures(self, user_id: UUID) -> None:
        for email, user in self._by_email.items():
            if user.id == user_id:
                self._by_email[email] = dataclasses.replace(
                    user, failed_login_attempts=0, locked_until=None, lockout_cycles=0
                )

    async def get_by_external_id(self, external_id: str) -> User | None:
        return next((u for u in self._by_email.values() if u.external_id == external_id), None)

    async def list(self, *, offset: int, limit: int) -> list[User]:
        users = sorted(self._by_email.values(), key=lambda u: (u.created_at, str(u.id)))
        return users[offset : offset + limit]

    async def update_scim_identity(
        self, user_id: UUID, email: str, external_id: str | None
    ) -> User:
        for old_email, user in self._by_email.items():
            if user.id == user_id:
                updated = dataclasses.replace(user, email=email, external_id=external_id)
                del self._by_email[old_email]
                self._by_email[email] = updated
                return updated
        raise AssertionError(f"no user {user_id}")

    async def delete(self, user_id: UUID) -> None:
        for email, user in list(self._by_email.items()):
            if user.id == user_id:
                del self._by_email[email]


class FakeInviteRepository:
    def __init__(self) -> None:
        self._by_id: dict[UUID, Invite] = {}

    async def add(self, invite: Invite) -> Invite:
        self._by_id[invite.id] = invite
        return invite

    async def get_by_token_hash(self, token_hash: str) -> Invite | None:
        return next((i for i in self._by_id.values() if i.token_hash == token_hash), None)

    async def mark_used(self, invite_id: UUID, used_at: datetime) -> bool:
        # Mirror the real repo's conditional UPDATE (consume only if not yet used);
        # expiry is enforced in the service before mark_used is called.
        invite = self._by_id.get(invite_id)
        if invite is None or invite.used_at is not None:
            return False
        self._by_id[invite_id] = dataclasses.replace(invite, used_at=used_at)
        return True


class FakePasswordResetRepository:
    def __init__(self) -> None:
        self._by_id: dict[UUID, PasswordReset] = {}

    async def add(self, reset: PasswordReset) -> PasswordReset:
        self._by_id[reset.id] = reset
        return reset

    async def get_by_token_hash(self, token_hash: str) -> PasswordReset | None:
        return next((r for r in self._by_id.values() if r.token_hash == token_hash), None)

    async def mark_used(self, reset_id: UUID, used_at: datetime) -> bool:
        reset = self._by_id.get(reset_id)
        if reset is None or reset.used_at is not None:
            return False
        self._by_id[reset_id] = dataclasses.replace(reset, used_at=used_at)
        return True


class FakeTeamMembershipRepository:
    """Minimal fake — only what user deletion/registration needs."""

    def __init__(self) -> None:
        self._items: list = []

    async def add(self, membership):  # noqa: ANN001, ANN201
        self._items.append(membership)
        return membership

    async def list_by_user(self, user_id: UUID, *, limit: int = 50, offset: int = 0):  # noqa: ANN201
        matches = [m for m in self._items if m.user_id == user_id]
        return matches[offset : offset + limit]


class FakeAPIKeyRepository:
    """Minimal fake — only the creator lookup used by the delete guard."""

    def __init__(self) -> None:
        self._items: list = []

    async def list_by_creator(self, created_by: UUID, *, limit: int = 50, offset: int = 0):  # noqa: ANN201
        matches = [k for k in self._items if k.created_by == created_by]
        return matches[offset : offset + limit]


@pytest.fixture
def service() -> UserService:
    return UserService(
        transaction=FakeTransaction(),
        users=FakeUserRepository(),
        invites=FakeInviteRepository(),
        password_resets=FakePasswordResetRepository(),
    )


def _account(email: str, *, is_admin: bool = False) -> User:
    from litestar_gateway.domain.password import hash_password

    return User(
        id=uuid4(),
        email=email,
        password_hash=hash_password("Passw0rd!"),
        is_admin=is_admin,
        created_at=datetime.now(UTC),
    )
