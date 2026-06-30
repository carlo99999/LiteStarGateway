"""Application service for users and invites — depends only on ports.

Use cases:
  * ensure_admin  — bootstrap: create the admin from MASTER_KEY if the table is empty.
  * create_invite — issue a single-use invite token (admin action).
  * register      — sign up with a valid invite token.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from uuid import UUID, uuid4

from litestar_test.domain.entities import Invite, IssuedInvite, User
from litestar_test.domain.exceptions import (
    EmailAlreadyRegistered,
    InvalidCredentials,
    InvalidInvite,
    MasterKeyMissing,
    WeakPassword,
)
from litestar_test.domain.key_generator import hash_key
from litestar_test.domain.password import hash_password, verify_password
from litestar_test.domain.ports import InviteRepository, UserRepository

_INVITE_TOKEN_BYTES = 32


def _now() -> datetime:
    return datetime.now(UTC)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


class UserService:
    def __init__(self, users: UserRepository, invites: InviteRepository) -> None:
        self._users = users
        self._invites = invites

    async def ensure_admin(self, admin_email: str, master_key: str | None) -> None:
        """Bootstrap the first user. Raises if the table is empty and no key is set."""
        if await self._users.count() > 0:
            return
        if not master_key:
            raise MasterKeyMissing(
                "No users exist and MASTER_KEY is not set; cannot bootstrap admin."
            )
        admin = User(
            id=uuid4(),
            email=_normalize_email(admin_email),
            password_hash=hash_password(master_key),
            is_admin=True,
            created_at=_now(),
        )
        await self._users.add(admin)

    async def create_invite(self) -> IssuedInvite:
        token = secrets.token_urlsafe(_INVITE_TOKEN_BYTES)
        invite = Invite(
            id=uuid4(),
            token_hash=hash_key(token),
            created_at=_now(),
            used_at=None,
        )
        stored = await self._invites.add(invite)
        return IssuedInvite(invite=stored, token=token)

    def __check_if_password_has_some_complexity(self, password: str) -> None:
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters long")
        if not any(c.islower() for c in password):
            raise ValueError("Password must contain at least one lowercase letter")
        if not any(c.isupper() for c in password):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.isdigit() for c in password):
            raise ValueError("Password must contain at least one digit")
        if not any(c in "!@#$%^&*()-_=+[{]}\\|;:'\",<.>/?`~" for c in password):
            raise ValueError("Password must contain at least one special character")

    async def register(self, invite_token: str, email: str, password: str) -> User:
        invite = await self._invites.get_by_token_hash(hash_key(invite_token))
        if invite is None or not invite.is_usable:
            raise InvalidInvite("Invite token is invalid or already used")

        # Validate the password before consuming the invite, so a legitimate
        # typo does not burn the invite (and it leaks nothing about the email).
        try:
            self.__check_if_password_has_some_complexity(password)
        except ValueError as e:
            raise WeakPassword(str(e)) from e

        # Consume the invite atomically *before* the email check, so two
        # concurrent signups can never reuse one invite AND so probing whether an
        # email exists costs a single-use, admin-issued invite per attempt
        # (account-enumeration is bounded by invite scarcity).
        if not await self._invites.mark_used(invite.id, _now()):
            raise InvalidInvite("Invite token is invalid or already used")

        email = _normalize_email(email)
        if await self._users.get_by_email(email) is not None:
            raise EmailAlreadyRegistered(email)

        return await self._users.add(
            User(
                id=uuid4(),
                email=email,
                password_hash=hash_password(password),
                is_admin=False,
                created_at=_now(),
            )
        )

    async def authenticate(self, email: str, password: str) -> User:
        """Verify login credentials. Raises InvalidCredentials on any mismatch."""
        user = await self._users.get_by_email(_normalize_email(email))
        if user is None or not verify_password(password, user.password_hash):
            raise InvalidCredentials("Invalid email or password")
        return user

    async def get_by_id(self, user_id: UUID) -> User | None:
        return await self._users.get(user_id)

    async def logout(self, user: User) -> None:
        """Invalidate all of the user's existing JWTs by bumping token_version."""
        await self._users.increment_token_version(user.id)
