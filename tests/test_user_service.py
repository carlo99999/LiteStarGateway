"""Unit tests for UserService using in-memory fake repositories."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from litestar_gateway.application.user_service import UserService
from litestar_gateway.domain.entities import ExternalIdentity, Invite, PasswordReset, User
from litestar_gateway.domain.exceptions import (
    EmailAlreadyRegistered,
    InvalidCredentials,
    InvalidInvite,
    InvalidPasswordReset,
    MasterKeyMissing,
    PermissionDenied,
    UserNotFound,
    WeakPassword,
)


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

    async def set_active(self, user_id: UUID, is_active: bool) -> None:
        for email, user in self._by_email.items():
            if user.id == user_id:
                tv = user.token_version + (0 if is_active else 1)
                self._by_email[email] = dataclasses.replace(
                    user, is_active=is_active, token_version=tv
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


async def test_set_user_active_disables_and_revokes(service: UserService) -> None:
    admin = _account("admin@b.com", is_admin=True)
    target = _account("bob@b.com")
    await service._users.add(admin)
    await service._users.add(target)

    updated = await service.set_user_active(admin, target.id, is_active=False)
    assert updated.is_active is False
    assert updated.token_version == target.token_version + 1  # sessions revoked


async def test_set_user_active_requires_platform_admin(service: UserService) -> None:
    non_admin = _account("nope@b.com")
    other = _account("other@b.com")
    await service._users.add(non_admin)
    await service._users.add(other)
    with pytest.raises(PermissionDenied):
        await service.set_user_active(non_admin, other.id, is_active=False)


async def test_set_user_active_forbids_self_disable(service: UserService) -> None:
    admin = _account("admin@b.com", is_admin=True)
    await service._users.add(admin)
    with pytest.raises(PermissionDenied):
        await service.set_user_active(admin, admin.id, is_active=False)


async def test_disabled_user_cannot_authenticate(service: UserService) -> None:
    admin = _account("admin@b.com", is_admin=True)
    target = _account("bob@b.com")
    await service._users.add(admin)
    await service._users.add(target)
    await service.set_user_active(admin, target.id, is_active=False)
    with pytest.raises(InvalidCredentials):
        await service.authenticate("bob@b.com", "Passw0rd!")


async def test_set_user_admin_promotes_and_demotes(service: UserService) -> None:
    admin = _account("admin@b.com", is_admin=True)
    target = _account("bob@b.com")
    await service._users.add(admin)
    await service._users.add(target)

    promoted = await service.set_user_admin(admin, target.id, is_admin=True)
    assert promoted.is_admin is True
    demoted = await service.set_user_admin(admin, target.id, is_admin=False)
    assert demoted.is_admin is False


async def test_set_user_admin_requires_platform_admin(service: UserService) -> None:
    non_admin = _account("nope@b.com")
    other = _account("other@b.com")
    await service._users.add(non_admin)
    await service._users.add(other)
    with pytest.raises(PermissionDenied):
        await service.set_user_admin(non_admin, other.id, is_admin=True)


async def test_set_user_admin_forbids_changing_own_role(service: UserService) -> None:
    # An admin demoting themselves is a self-lockout footgun (mirrors set_active).
    admin = _account("admin@b.com", is_admin=True)
    await service._users.add(admin)
    with pytest.raises(PermissionDenied):
        await service.set_user_admin(admin, admin.id, is_admin=False)


async def test_set_user_admin_unknown_user(service: UserService) -> None:
    admin = _account("admin@b.com", is_admin=True)
    await service._users.add(admin)
    with pytest.raises(UserNotFound):
        await service.set_user_admin(admin, uuid4(), is_admin=True)


def _identity(subject: str, *, groups: tuple[str, ...] = ()) -> ExternalIdentity:
    return ExternalIdentity(
        subject=subject, email=f"{subject}@b.com", email_verified=True, groups=groups
    )


async def test_sso_new_user_gets_admin_from_group(service: UserService) -> None:
    user = await service.upsert_sso_user(_identity("s1"), group_admin=True, default_admin=False)
    assert user.is_admin is True


async def test_sso_new_user_gets_admin_from_default_role(service: UserService) -> None:
    # DEFAULT_ROLE=admin seeds a brand-new account even without an admin group.
    user = await service.upsert_sso_user(_identity("s2"), group_admin=False, default_admin=True)
    assert user.is_admin is True


async def test_sso_new_user_defaults_to_member(service: UserService) -> None:
    user = await service.upsert_sso_user(_identity("s3"), group_admin=False, default_admin=False)
    assert user.is_admin is False


async def test_sso_relogin_never_downgrades_admin(service: UserService) -> None:
    # A manual (or prior) admin grant survives a re-login without the admin group —
    # sync is upgrade-only; only the platform-admin endpoint demotes.
    await service.upsert_sso_user(_identity("s4"), group_admin=True, default_admin=False)
    again = await service.upsert_sso_user(_identity("s4"), group_admin=False, default_admin=False)
    assert again.is_admin is True


async def test_sso_relogin_upgrades_member_in_admin_group(service: UserService) -> None:
    await service.upsert_sso_user(_identity("s5"), group_admin=False, default_admin=False)
    again = await service.upsert_sso_user(_identity("s5"), group_admin=True, default_admin=False)
    assert again.is_admin is True


async def test_ensure_admin_raises_when_empty_and_no_master_key(
    service: UserService,
) -> None:
    with pytest.raises(MasterKeyMissing):
        await service.ensure_admin("admin@example.com", master_key=None)


async def test_ensure_admin_creates_admin_with_master_key(service: UserService) -> None:
    await service.ensure_admin("admin@example.com", master_key="secret")
    admin = await service._users.get_by_email("admin@example.com")
    assert admin is not None
    assert admin.is_admin is True
    assert admin.password_hash != "secret"  # stored hashed


async def test_ensure_admin_is_idempotent_when_users_exist(
    service: UserService,
) -> None:
    await service.ensure_admin("admin@example.com", master_key="secret")
    # A second call with no key must NOT raise, because users already exist.
    await service.ensure_admin("admin@example.com", master_key=None)
    assert await service._users.count() == 1


async def test_register_requires_valid_invite(service: UserService) -> None:
    with pytest.raises(InvalidInvite):
        await service.register("bad-token", "a@b.com", "PASSw0rd!")


async def test_register_consumes_invite(service: UserService) -> None:
    issued = await service.create_invite()
    await service.register(issued.token, "a@b.com", "Passw0rd!")
    with pytest.raises(InvalidInvite):
        await service.register(issued.token, "c@d.com", "Passw0rd!")


async def test_register_rejects_expired_invite() -> None:
    invites = FakeInviteRepository()
    svc = UserService(
        transaction=FakeTransaction(),
        users=FakeUserRepository(),
        invites=invites,
        password_resets=FakePasswordResetRepository(),
    )
    issued = await svc.create_invite()
    # Force the stored invite past its expiry.
    invites._by_id[issued.invite.id] = dataclasses.replace(
        invites._by_id[issued.invite.id], expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    with pytest.raises(InvalidInvite):
        await svc.register(issued.token, "late@b.com", "Passw0rd!")


async def test_create_invite_sets_future_expiry(service: UserService) -> None:
    issued = await service.create_invite()
    assert issued.invite.expires_at > datetime.now(UTC)


async def test_register_rejects_duplicate_email(service: UserService) -> None:
    i1 = await service.create_invite()
    i2 = await service.create_invite()
    await service.register(i1.token, "dup@b.com", "Passw0rd!")
    with pytest.raises(EmailAlreadyRegistered):
        await service.register(i2.token, "dup@b.com", "Passw0rd!")


@pytest.mark.parametrize(
    "password",
    [
        "Short1!",  # too short (7 chars)
        "alllowercase1!",  # no uppercase
        "ALLUPPERCASE1!",  # no lowercase
        "NoDigitsHere!",  # no digit
        "NoSpecial1A",  # no special character
    ],
)
async def test_register_rejects_weak_password(service: UserService, password: str) -> None:
    issued = await service.create_invite()
    with pytest.raises(WeakPassword):
        await service.register(issued.token, "weak@b.com", password)


async def _admin(service: UserService) -> User:
    await service.ensure_admin("admin@example.com", master_key="secret")
    admin = await service._users.get_by_email("admin@example.com")
    assert admin is not None
    return admin


async def _make_user(service: UserService, email: str, password: str) -> None:
    issued = await service.create_invite()
    await service.register(issued.token, email, password)


async def test_create_password_reset_requires_admin(service: UserService) -> None:
    await _make_user(service, "u@b.com", "Passw0rd!")
    non_admin = await service._users.get_by_email("u@b.com")
    assert non_admin is not None
    with pytest.raises(PermissionDenied):
        await service.create_password_reset(non_admin, "u@b.com")


async def test_create_password_reset_unknown_email(service: UserService) -> None:
    admin = await _admin(service)
    with pytest.raises(UserNotFound):
        await service.create_password_reset(admin, "ghost@b.com")


async def test_reset_password_changes_password(service: UserService) -> None:
    admin = await _admin(service)
    await _make_user(service, "u@b.com", "OldPassw0rd!")

    issued = await service.create_password_reset(admin, "u@b.com")
    await service.reset_password(issued.token, "NewPassw0rd!")

    # New password works; old one no longer does.
    await service.authenticate("u@b.com", "NewPassw0rd!")
    with pytest.raises(InvalidCredentials):
        await service.authenticate("u@b.com", "OldPassw0rd!")


async def test_reset_token_is_single_use(service: UserService) -> None:
    admin = await _admin(service)
    await _make_user(service, "u@b.com", "Passw0rd!")
    issued = await service.create_password_reset(admin, "u@b.com")
    await service.reset_password(issued.token, "NewPassw0rd!")
    with pytest.raises(InvalidPasswordReset):
        await service.reset_password(issued.token, "Another1!")


async def test_reset_password_rejects_invalid_token(service: UserService) -> None:
    with pytest.raises(InvalidPasswordReset):
        await service.reset_password("nope", "NewPassw0rd!")


async def test_reset_password_rejects_weak_password(service: UserService) -> None:
    admin = await _admin(service)
    await _make_user(service, "u@b.com", "Passw0rd!")
    issued = await service.create_password_reset(admin, "u@b.com")
    with pytest.raises(WeakPassword):
        await service.reset_password(issued.token, "weak")


async def test_ensure_admin_tolerates_concurrent_replica_bootstrap() -> None:
    """Two replicas racing ensure_admin on an empty table: the loser's add() hits
    the unique email constraint (EmailAlreadyRegistered) and must not crash startup."""

    class RacedUserRepository(FakeUserRepository):
        async def count(self) -> int:
            return 0  # both replicas observed an empty table

        async def add(self, user: User) -> User:
            if user.email in self._by_email:
                raise EmailAlreadyRegistered(user.email)
            return await super().add(user)

    users = RacedUserRepository()
    service = UserService(
        transaction=FakeTransaction(),
        users=users,
        invites=FakeInviteRepository(),
        password_resets=FakePasswordResetRepository(),
    )
    await service.ensure_admin("admin@example.com", master_key="secret")
    # Second replica loses the insert race; ensure_admin must swallow the conflict.
    await service.ensure_admin("admin@example.com", master_key="secret")
    assert await users.get_by_email("admin@example.com") is not None


async def test_async_password_wrappers_roundtrip() -> None:
    """The a* variants offload Argon2 to a thread but must match the sync hasher."""
    from litestar_gateway.domain.password import ahash_password, averify_password, verify_password

    hashed = await ahash_password("Passw0rd!")
    assert verify_password("Passw0rd!", hashed)
    assert await averify_password("Passw0rd!", hashed)
    assert not await averify_password("wrong-password", hashed)
