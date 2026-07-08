"""Application service for users and invites — depends only on ports.

Use cases:
  * ensure_admin  — bootstrap: create the admin from MASTER_KEY if the table is empty.
  * create_invite — issue a single-use invite token (admin action).
  * register      — sign up with a valid invite token.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from litestar_gateway.domain.entities import (
    ExternalIdentity,
    Invite,
    IssuedInvite,
    IssuedPasswordReset,
    PasswordReset,
    User,
)
from litestar_gateway.domain.exceptions import (
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
from litestar_gateway.domain.key_generator import hash_key
from litestar_gateway.domain.password import ahash_password, averify_password
from litestar_gateway.domain.ports import (
    APIKeyRepository,
    InviteRepository,
    PasswordResetRepository,
    Transaction,
    UserRepository,
)

logger = logging.getLogger("litestar_gateway.auth")

_INVITE_TOKEN_BYTES = 32
_RESET_TOKEN_BYTES = 32
_RESET_TTL = timedelta(hours=24)
_INVITE_TTL = timedelta(hours=72)

# Per-account brute-force lockout: after MAX_FAILED_LOGINS consecutive wrong
# passwords, password logins are rejected for a window that doubles on every
# consecutive lock cycle (capped at MAX_LOCKOUT_DURATION). An attacker who
# keeps re-locking the account can still deny password login, but each cycle
# gets rarer and noisier (a WARNING per lock), SSO is unaffected, and a
# platform admin can lift the lock at any time (DELETE /users/{id}/lock).
# A quiet LOCKOUT_DECAY after the last lock expires resets the escalation.
# Complements the per-IP rate limit, which a distributed attack slips under.
MAX_FAILED_LOGINS = 5
LOCKOUT_DURATION = timedelta(minutes=15)
MAX_LOCKOUT_DURATION = timedelta(hours=24)
LOCKOUT_DECAY = timedelta(hours=24)


def lockout_duration(cycles: int) -> timedelta:
    """Lock duration after `cycles` prior consecutive lock cycles: the base
    doubled per cycle, hard-capped so a victim is never locked out for days."""
    return min(LOCKOUT_DURATION * (2 ** min(cycles, 16)), MAX_LOCKOUT_DURATION)


# Decoy Argon2 hash (of a discarded random string — not a secret). Verified on
# the failure branches that would otherwise skip password hashing entirely
# (unknown email, locked account), so response timing doesn't reveal whether
# an email is registered or an account is locked.
# pragma: allowlist nextline secret
_DECOY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4"
    "$TmkXiElHRqSn0xJMTpMPtw$ILw5P96j7Z+FJLdu0mmefGvqBZ2e7nWXLh6QGavdq88"
)


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
        transaction: Transaction,
        api_keys: APIKeyRepository | None = None,
    ) -> None:
        self._users = users
        self._invites = invites
        self._password_resets = password_resets
        self._transaction = transaction
        self._api_keys = api_keys

    @asynccontextmanager
    async def _unit_of_work(self) -> AsyncGenerator[None]:
        """Commit staged writes once on success; roll back on any failure."""
        try:
            yield
            await self._transaction.commit()
        except Exception:
            await self._transaction.rollback()
            raise

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
        try:
            await self._users.add(admin)
        except EmailAlreadyRegistered:
            # Multi-replica startup race: another replica bootstrapped between our
            # count() and add(). The unique email constraint is the arbiter — the
            # admin exists, which is all this hook guarantees.
            return

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
        # (account-enumeration is bounded by invite scarcity). Deliberately NOT
        # a unit of work with the user insert: rolling the consumption back on
        # failure would make that probing free.
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
        """Verify login credentials. Raises InvalidCredentials on any mismatch.

        Every failure path returns the same generic error: whether the email is
        unknown, the password wrong, the account disabled or temporarily locked
        must not be observable from the response."""
        user = await self._users.get_by_email(_normalize_email(email))
        if user is None:
            # Burn the same Argon2 cost as a real verification (timing parity).
            await averify_password(password, _DECOY_PASSWORD_HASH)
            raise InvalidCredentials("Invalid email or password")
        # A locked account rejects even the correct password until the lock
        # expires — otherwise the lock would be a password oracle.
        if user.locked_until is not None and _now() < user.locked_until:
            await averify_password(password, _DECOY_PASSWORD_HASH)
            raise InvalidCredentials("Invalid email or password")
        if not await averify_password(password, user.password_hash):
            await self._register_login_failure(user)
            raise InvalidCredentials("Invalid email or password")
        # A disabled account cannot log in; same generic error (don't reveal state).
        if not user.is_active:
            raise InvalidCredentials("Invalid email or password")
        if user.failed_login_attempts or user.locked_until is not None:
            await self._users.clear_login_failures(user.id)
        return user

    async def _register_login_failure(self, user: User) -> None:
        """Count a wrong password; lock the account once the threshold is hit.

        Consecutive lock cycles escalate: the duration doubles per cycle
        (capped), so re-locking the victim the moment a lock expires costs the
        attacker progressively more waiting per denial-of-service hour. A
        quiet LOCKOUT_DECAY since the last lock expired resets the curve."""
        count = await self._users.register_failed_login(user.id)
        if count < MAX_FAILED_LOGINS:
            return
        now = _now()
        cycles = user.lockout_cycles
        decayed = user.locked_until is not None and now - user.locked_until > LOCKOUT_DECAY
        if decayed:
            cycles = 0
        duration = lockout_duration(cycles)
        # lockout_cycles is incremented in-database (reset to 1 on decay); the
        # duration still uses the snapshot cycle for the exponential.
        await self._users.set_login_lock(user.id, now + duration, reset_cycles=decayed)
        # Escalating cycles on one account are the signature of a sustained
        # lockout attack — make each one visible to operators.
        logger.warning(
            "account locked after %d failed logins: user=%s cycle=%d duration=%s",
            count,
            user.id,
            cycles + 1,
            duration,
        )

    async def unlock_user(self, actor: User, user_id: UUID) -> None:
        """Platform-admin lifts a login lock (and resets the escalation) — the
        recovery lever when an attacker keeps re-locking a victim's account."""
        if not actor.is_admin:
            raise PermissionDenied("Platform admin privileges required")
        if await self._users.get(user_id) is None:
            raise UserNotFound(str(user_id))
        await self._users.clear_login_failures(user_id)

    async def get_by_id(self, user_id: UUID) -> User | None:
        return await self._users.get(user_id)

    async def logout(self, user: User) -> None:
        """Invalidate all of the user's existing JWTs by bumping token_version."""
        await self._users.increment_token_version(user.id)

    async def upsert_sso_user(
        self, identity: ExternalIdentity, *, group_admin: bool, default_admin: bool
    ) -> User:
        """Resolve (or JIT-provision) the local account for a verified SSO identity.

        Accounts are bound to the IdP subject (`sub`), which is stable across the
        user's email changes. An email is adopted only when the IdP asserts it
        verified, and never when it already belongs to a *different* subject (so a
        recycled/spoofed email cannot take over another account).

        The platform-admin flag is **upgrade-only**. Membership in an admin group
        (`group_admin`) grants admin, and a brand-new account additionally honours
        the DEFAULT_ROLE fallback (`default_admin`) — but re-login never *revokes*
        admin. Demotion is the explicit job of the platform-admin endpoint, so a
        manual grant (or revoke) is never silently undone by the next SSO login, and
        leaving the admin group does not strip a role granted another way.
        """
        if not identity.email or not identity.email_verified:
            raise SSOEmailNotVerified(identity.email or "<none>")
        normalized = _normalize_email(identity.email)

        existing = await self._users.get_by_sso_subject(identity.subject)
        if existing is None:
            # Not yet bound to this subject: JIT-create with the first-login role, or
            # adopt a pre-existing password-only account (which keeps its own role;
            # DEFAULT_ROLE seeds only genuinely new accounts).
            created = await self._resolve_or_create_sso_user(
                identity, normalized, group_admin or default_admin
            )
            if created.sso_subject == identity.subject:
                return created  # freshly JIT-created; role already set
            existing = created  # a password-only account to link
        # Upgrade-only re-sync: group membership can promote, re-login never demotes.
        return await self._users.bind_sso(
            existing.id, identity.subject, existing.is_admin or group_admin
        )

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
        # Disabling revokes the user's personal keys too: like their JWT
        # sessions, credentials that represent the person must not outlive their
        # access. Service-principal keys are untouched (a machine identity
        # doesn't belong to this user). Revoke the keys BEFORE flipping the
        # account (the two are separate commits): should the process die between
        # them, the higher-risk credential — the leaked/forgotten key — is
        # already dead, rather than left live against a still-active account.
        if not is_active and self._api_keys is not None:
            await self._api_keys.revoke_personal_keys_for_user(user_id, _now())
        # Mark admin disables so SCIM cannot resurrect the account on an IdP
        # full-sync (see ScimService.update_user); reactivation clears the mark.
        await self._users.set_active(
            user_id, is_active, deactivated_by=None if is_active else "admin"
        )
        updated = await self._users.get(user_id)
        if updated is None:  # pragma: no cover - just set it
            raise UserNotFound(str(user_id))
        return updated

    async def set_user_auditor(self, actor: User, user_id: UUID, is_auditor: bool) -> User:
        """Platform-admin grants or revokes the read-only platform-auditor role.
        No self-guard: unlike the admin flag, dropping auditor from yourself
        cannot lock anyone out of anything."""
        if not actor.is_admin:
            raise PermissionDenied("Platform admin privileges required")
        if await self._users.get(user_id) is None:
            raise UserNotFound(str(user_id))
        await self._users.set_auditor(user_id, is_auditor)
        updated = await self._users.get(user_id)
        if updated is None:  # pragma: no cover - just set it
            raise UserNotFound(str(user_id))
        return updated

    async def set_user_admin(self, actor: User, user_id: UUID, is_admin: bool) -> User:
        """Platform-admin grants or revokes another user's platform-admin role. This
        is the only path that *demotes* an admin — SSO group / DEFAULT_ROLE sync is
        upgrade-only. An admin cannot change their own role (a self-lockout footgun,
        matching the self-guard on enable/disable)."""
        if not actor.is_admin:
            raise PermissionDenied("Platform admin privileges required")
        if actor.id == user_id:
            raise PermissionDenied("Cannot change your own admin role")
        if await self._users.get(user_id) is None:
            raise UserNotFound(str(user_id))
        await self._users.set_admin(user_id, is_admin)
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

        # One unit of work: consuming the token and writing the password
        # persist together or not at all — a failure after the token was
        # consumed must not strand the user with a burned token. The
        # conditional UPDATE still guards double-redemption (the row stays
        # locked until commit).
        password_hash = await ahash_password(new_password)
        async with self._unit_of_work():
            if not await self._password_resets.mark_used(reset.id, _now()):
                raise InvalidPasswordReset("Reset token is invalid, used, or expired")
            await self._users.set_password(reset.user_id, password_hash)
