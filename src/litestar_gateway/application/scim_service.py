"""Application service for SCIM provisioning — depends only on ports.

Two responsibilities:
  * provisioning-token lifecycle — platform admins mint/list/revoke the bearer
    token the IdP presents on /scim/v2 (hashed at rest, like invites);
  * IdP-driven user lifecycle — create/read/list/update/deactivate local
    accounts on behalf of the IdP.

Deactivation mirrors the admin lever (`UserService.set_user_active`): existing
sessions are revoked (token_version bump) and the user's personal API keys die
with their access. Platform admins are the one exception — the gateway, not the
IdP, governs admins (same philosophy as the upgrade-only SSO admin flag), so a
SCIM deactivation of an admin account is refused — as is any change to an
admin's identity (userName/externalId).
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from uuid import UUID, uuid4

from litestar_gateway.domain.entities import IssuedScimToken, ScimToken, User
from litestar_gateway.domain.exceptions import (
    InvalidScimToken,
    PermissionDenied,
    ScimTokenNotFound,
    UserNotFound,
)
from litestar_gateway.domain.key_generator import hash_key
from litestar_gateway.domain.password import ahash_password
from litestar_gateway.domain.ports import (
    APIKeyRepository,
    ScimTokenRepository,
    UserRepository,
)

_SCIM_TOKEN_BYTES = 32
_SCIM_TOKEN_PREFIX = "scim_"


def _now() -> datetime:
    return datetime.now(UTC)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


class ScimService:
    def __init__(
        self,
        users: UserRepository,
        tokens: ScimTokenRepository,
        api_keys: APIKeyRepository | None = None,
    ) -> None:
        self._users = users
        self._tokens = tokens
        self._api_keys = api_keys

    # ── Provisioning tokens (platform-admin surface) ─────────────────────────

    async def create_token(self, actor: User, name: str) -> IssuedScimToken:
        """Mint a provisioning token; the plaintext is returned exactly once."""
        self._require_admin(actor)
        plaintext = _SCIM_TOKEN_PREFIX + secrets.token_urlsafe(_SCIM_TOKEN_BYTES)
        stored = await self._tokens.add(
            ScimToken(
                id=uuid4(),
                name=name,
                token_hash=hash_key(plaintext),
                created_at=_now(),
                revoked_at=None,
            )
        )
        return IssuedScimToken(scim_token=stored, token=plaintext)

    async def list_tokens(self, actor: User) -> list[ScimToken]:
        self._require_admin(actor)
        return await self._tokens.list()

    async def revoke_token(self, actor: User, token_id: UUID) -> None:
        self._require_admin(actor)
        if not await self._tokens.revoke(token_id, _now()):
            raise ScimTokenNotFound(str(token_id))

    async def authenticate(self, token: str) -> ScimToken:
        """Resolve a presented bearer token; rejects unknown and revoked ones."""
        record = await self._tokens.get_by_token_hash(hash_key(token))
        if record is None or not record.is_usable:
            raise InvalidScimToken("SCIM token is invalid or revoked")
        return record

    def _require_admin(self, actor: User) -> None:
        if not actor.is_admin:
            raise PermissionDenied("Platform admin privileges required")

    # ── IdP-driven user lifecycle ────────────────────────────────────────────

    async def create_user(self, *, email: str, external_id: str | None, active: bool) -> User:
        """Provision an account for the IdP. SCIM accounts get an unknowable
        random password — they authenticate via SSO only. Raises
        EmailAlreadyRegistered when the userName (or externalId) is taken."""
        return await self._users.add(
            User(
                id=uuid4(),
                email=_normalize_email(email),
                password_hash=await ahash_password(secrets.token_urlsafe(_SCIM_TOKEN_BYTES)),
                is_admin=False,
                created_at=_now(),
                is_active=active,
                external_id=external_id,
            )
        )

    async def get_user(self, user_id: UUID) -> User:
        user = await self._users.get(user_id)
        if user is None:
            raise UserNotFound(str(user_id))
        return user

    async def find_users(
        self,
        *,
        email: str | None = None,
        external_id: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[list[User], int]:
        """Users matching an equality filter (email/externalId — at most one is
        set), or a stable page of all users. Returns (rows, total matches)."""
        if email is not None:
            user = await self._users.get_by_email(_normalize_email(email))
            return ([user], 1) if user else ([], 0)
        if external_id is not None:
            user = await self._users.get_by_external_id(external_id)
            return ([user], 1) if user else ([], 0)
        rows = await self._users.list(offset=offset, limit=limit)
        return rows, await self._users.count()

    async def update_user(
        self,
        user_id: UUID,
        *,
        email: str | None = None,
        external_id: str | None = None,
        active: bool | None = None,
    ) -> User:
        """Apply the final attribute state computed from a PUT/PATCH. None means
        "leave unchanged" (pragmatic PUT: IdPs resend the full resource anyway,
        and clearing externalId/userName on omission would only break adoption).
        """
        user = await self.get_user(user_id)
        target_active = user.is_active if active is None else active
        if user.is_admin and not target_active:
            raise PermissionDenied(
                "SCIM cannot deactivate a platform admin; demote via the admin API first"
            )
        # SCIM may only undo its own deactivations: an admin disabled the account
        # for cause, and a routine IdP full-sync with active:true must not
        # silently resurrect it (same philosophy as the admin guard above).
        if target_active and not user.is_active and user.deactivated_by == "admin":
            raise PermissionDenied(
                "SCIM cannot reactivate an account disabled by an admin; "
                "re-enable via the admin API first"
            )

        new_email = _normalize_email(email) if email else user.email
        new_external_id = user.external_id if external_id is None else external_id
        if new_email != user.email or new_external_id != user.external_id:
            if user.is_admin:
                raise PermissionDenied(
                    "SCIM cannot change a platform admin's identity; demote via the admin API first"
                )
            user = await self._users.update_scim_identity(user_id, new_email, new_external_id)

        if target_active != user.is_active:
            # Same ordering rationale as UserService.set_user_active: kill the
            # riskier credential (personal keys) before flipping the account.
            if not target_active and self._api_keys is not None:
                await self._api_keys.revoke_personal_keys_for_user(user_id, _now())
            await self._users.set_active(
                user_id, target_active, deactivated_by=None if target_active else "scim"
            )
            user = await self.get_user(user_id)
        return user

    async def deactivate_user(self, user_id: UUID) -> User:
        """SCIM DELETE: deactivate rather than erase (audit/usage rows must keep
        their actor), revoking sessions and personal keys like a manual disable."""
        return await self.update_user(user_id, active=False)
