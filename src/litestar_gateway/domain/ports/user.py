"""Port — user persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from litestar_gateway.domain.entities import User


class UserRepository(Protocol):
    """Persistence port for users."""

    async def add(self, user: User) -> User: ...

    async def get(self, user_id: UUID) -> User | None: ...

    async def get_by_email(self, email: str) -> User | None: ...

    async def get_by_sso_subject(self, subject: str) -> User | None: ...

    async def get_by_external_id(self, external_id: str) -> User | None: ...

    async def list(self, *, offset: int, limit: int) -> list[User]:
        """Users in a stable order (creation time) for paged listing."""
        ...

    async def update_scim_identity(
        self, user_id: UUID, email: str, external_id: str | None
    ) -> User:
        """Set the SCIM-managed identity attributes (email/externalId), returning
        the updated user. Raises EmailAlreadyRegistered on a uniqueness clash."""
        ...

    async def count(self) -> int: ...

    async def set_active(
        self, user_id: UUID, is_active: bool, *, deactivated_by: str | None = None
    ) -> None:
        """Enable/disable the account; disabling also bumps token_version to revoke
        the user's existing sessions. `deactivated_by` records which lever disabled
        the account ("admin" or "scim"); it is cleared on reactivation."""
        ...

    async def set_auditor(self, user_id: UUID, is_auditor: bool) -> None:
        """Grant/revoke the read-only platform-auditor role. Read live per
        request like the admin flag, so it takes effect immediately."""
        ...

    async def set_admin(self, user_id: UUID, is_admin: bool) -> None:
        """Grant/revoke the account's platform-admin role. Read live per request
        (not carried in the JWT), so it takes effect immediately — no token bump."""
        ...

    async def bind_sso(self, user_id: UUID, sso_subject: str, is_admin: bool) -> User:
        """Link an account to an IdP subject and set its admin flag to the value the
        caller computed. SSO role sync is upgrade-only (see UserService.upsert_sso_user),
        so the caller passes an already-merged flag; returns the updated user."""
        ...

    async def increment_token_version(self, user_id: UUID) -> None: ...

    async def set_password(self, user_id: UUID, password_hash: str) -> None:
        """Set a new password hash and bump token_version (revoking old JWTs)."""
        ...

    async def register_failed_login(self, user_id: UUID) -> int:
        """Atomically increment the failed-login counter; returns the new count."""
        ...

    async def set_login_lock(
        self, user_id: UUID, locked_until: datetime, *, reset_cycles: bool
    ) -> None:
        """Temporarily lock password logins and reset the failure counter. The
        consecutive lock-cycle count (which drives escalation) is incremented
        in-database, or reset to 1 when `reset_cycles` (a decayed prior lock)."""
        ...

    async def clear_login_failures(self, user_id: UUID) -> None:
        """Reset the failure counter, any lock, and the lock-cycle escalation
        (after a successful login or an admin unlock)."""
        ...
