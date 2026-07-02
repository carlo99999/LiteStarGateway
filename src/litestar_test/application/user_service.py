"""Application service for users and invites — depends only on ports.

Use cases:
  * ensure_admin  — bootstrap: create the admin from MASTER_KEY if the table is empty.
  * create_invite — issue a single-use invite token (admin action).
  * register      — sign up with a valid invite token.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from litestar_test.domain.entities import (
    ExternalIdentity,
    Invite,
    IssuedInvite,
    IssuedPasswordReset,
    PasswordReset,
    User,
)
from litestar_test.domain.exceptions import (
    EmailAlreadyRegistered,
    InvalidCredentials,
    InvalidInvite,
    InvalidPasswordReset,
    MasterKeyMissing,
    PermissionDenied,
    SSOEmailNotVerified,
    SSOIdentityConflict,
    UserNotFound,
    WeakPassword,
)
from litestar_test.domain.key_generator import hash_key
from litestar_test.domain.password import ahash_password, averify_password
from litestar_test.domain.ports import (
    InviteRepository,
    PasswordResetRepository,
    UserRepository,
)

_INVITE_TOKEN_BYTES = 32
_RESET_TOKEN_BYTES = 32
_RESET_TTL = timedelta(hours=24)
_INVITE_TTL = timedelta(hours=72)


def _now() -> datetime:
    return datetime.now(UTC)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


class UserService:
    def __init__(
        self,
        users: UserRepository,
        invites: InviteRepository,
        password_resets: PasswordResetRepository,
    ) -> None:
        self._users = users
        self._invites = invites
        self._password_resets = password_resets

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
            password_hash=await ahash_password(master_key),
            is_admin=True,
            created_at=_now(),
        )
        await self._users.add(admin)

    async def create_invite(self) -> IssuedInvite:
        token = secrets.token_urlsafe(_INVITE_TOKEN_BYTES)
        now = _now()
        invite = Invite(
            id=uuid4(),
            token_hash=hash_key(token),
            created_at=now,
            expires_at=now + _INVITE_TTL,
            used_at=None,
        )
        stored = await self._invites.add(invite)
        return IssuedInvite(invite=stored, token=token)

    def _check_password_complexity(self, password: str) -> None:
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
        if invite is None or not invite.is_usable(_now()):
            raise InvalidInvite("Invite token is invalid, expired, or already used")

        # Validate the password before consuming the invite, so a legitimate
        # typo does not burn the invite (and it leaks nothing about the email).
        try:
            self._check_password_complexity(password)
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
                password_hash=await ahash_password(password),
                is_admin=False,
                created_at=_now(),
            )
        )

    async def authenticate(self, email: str, password: str) -> User:
        """Verify login credentials. Raises InvalidCredentials on any mismatch."""
        user = await self._users.get_by_email(_normalize_email(email))
        if user is None or not await averify_password(password, user.password_hash):
            raise InvalidCredentials("Invalid email or password")
        # A disabled account cannot log in; same generic error (don't reveal state).
        if not user.is_active:
            raise InvalidCredentials("Invalid email or password")
        return user

    async def get_by_id(self, user_id: UUID) -> User | None:
        return await self._users.get(user_id)

    async def logout(self, user: User) -> None:
        """Invalidate all of the user's existing JWTs by bumping token_version."""
        await self._users.increment_token_version(user.id)

    async def upsert_sso_user(self, identity: ExternalIdentity, is_admin: bool) -> User:
        """Resolve (or JIT-provision) the local account for a verified SSO identity.

        Accounts are bound to the IdP subject (`sub`), which is stable across the
        user's email changes. An email is adopted only when the IdP asserts it
        verified, and never when it already belongs to a *different* subject (so a
        recycled/spoofed email cannot take over another account). The admin flag is
        re-synced from the IdP groups on every login — the IdP is the source of truth.
        """
        if not identity.email or not identity.email_verified:
            raise SSOEmailNotVerified(identity.email or "<none>")
        normalized = _normalize_email(identity.email)

        existing = await self._users.get_by_sso_subject(identity.subject)
        if existing is None:
            existing = await self._resolve_or_create_sso_user(identity, normalized, is_admin)
            if existing.sso_subject == identity.subject:
                # Freshly JIT-created with the right subject and role already set.
                return existing
        return await self._users.bind_sso(existing.id, identity.subject, is_admin)

    async def _resolve_or_create_sso_user(
        self, identity: ExternalIdentity, normalized_email: str, is_admin: bool
    ) -> User:
        """No account is bound to this subject yet: adopt the verified-email account
        if one exists (and is unlinked), else JIT-create. Raises on a subject clash."""
        by_email = await self._users.get_by_email(normalized_email)
        if by_email is not None:
            if by_email.sso_subject is not None:
                raise SSOIdentityConflict(normalized_email)
            return by_email  # link this previously password-only account
        try:
            # A random, unknowable password hash — SSO users authenticate only via SSO.
            return await self._users.add(
                User(
                    id=uuid4(),
                    email=normalized_email,
                    password_hash=await ahash_password(secrets.token_urlsafe(_INVITE_TOKEN_BYTES)),
                    is_admin=is_admin,
                    created_at=_now(),
                    sso_subject=identity.subject,
                )
            )
        except EmailAlreadyRegistered:
            # Concurrent first login for the same identity created the row between
            # our lookups — re-resolve and let the caller sync role via bind_sso.
            raced = await self._users.get_by_sso_subject(identity.subject)
            if raced is None:
                raced = await self._users.get_by_email(normalized_email)
            if raced is None or raced.sso_subject not in (None, identity.subject):
                raise SSOIdentityConflict(normalized_email) from None
            return raced

    async def set_user_active(self, actor: User, user_id: UUID, is_active: bool) -> User:
        """Platform-admin enables/disables another account. Disabling revokes the
        target's existing sessions. An admin cannot disable their own account (a
        lockout footgun)."""
        if not actor.is_admin:
            raise PermissionDenied("Platform admin privileges required")
        if actor.id == user_id:
            raise PermissionDenied("Cannot change your own active status")
        if await self._users.get(user_id) is None:
            raise UserNotFound(str(user_id))
        await self._users.set_active(user_id, is_active)
        updated = await self._users.get(user_id)
        if updated is None:  # pragma: no cover - just set it
            raise UserNotFound(str(user_id))
        return updated

    async def create_password_reset(self, actor: User, email: str) -> IssuedPasswordReset:
        """Platform-admin issues a single-use, expiring reset token for a user.

        The admin relays the token; the user redeems it and picks a new password,
        so the admin never learns it.
        """
        if not actor.is_admin:
            raise PermissionDenied("Platform admin privileges required")
        user = await self._users.get_by_email(_normalize_email(email))
        if user is None:
            raise UserNotFound(email)

        token = secrets.token_urlsafe(_RESET_TOKEN_BYTES)
        now = _now()
        stored = await self._password_resets.add(
            PasswordReset(
                id=uuid4(),
                user_id=user.id,
                token_hash=hash_key(token),
                created_at=now,
                expires_at=now + _RESET_TTL,
                used_at=None,
            )
        )
        return IssuedPasswordReset(reset=stored, token=token)

    async def reset_password(self, reset_token: str, new_password: str) -> None:
        """Redeem a reset token: validate, set the new password, revoke old JWTs."""
        reset = await self._password_resets.get_by_token_hash(hash_key(reset_token))
        if reset is None or not reset.is_usable(_now()):
            raise InvalidPasswordReset("Reset token is invalid, used, or expired")

        try:
            self._check_password_complexity(new_password)
        except ValueError as e:
            raise WeakPassword(str(e)) from e

        # Consume atomically before applying, so a token can't be used twice.
        if not await self._password_resets.mark_used(reset.id, _now()):
            raise InvalidPasswordReset("Reset token is invalid, used, or expired")

        await self._users.set_password(reset.user_id, await ahash_password(new_password))
