"""Application service — orchestrates the API key use cases.

Depends only on persistence and transaction ports plus pure domain logic,
never on SQLAlchemy or Litestar. This is the hexagon's core.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from litestar_gateway.domain.entities import APIKey, AuditEvent, IssuedKey, KeyScope
from litestar_gateway.domain.exceptions import (
    APIKeyNotFound,
    InvalidAPIKey,
    ManagementScopeRequiresServicePrincipal,
    PermissionDenied,
    ServicePrincipalNotFound,
)
from litestar_gateway.domain.key_generator import generate_key, hash_key
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.domain.ports import (
    APIKeyRepository,
    AuditLog,
    ServicePrincipalRepository,
    Transaction,
    UserRepository,
)

# Only persist last_used_at this often, to avoid a DB write on every request.
_LAST_USED_THROTTLE = timedelta(minutes=1)

# Rotation grace: after a rotate, the old key keeps working this long so callers
# can migrate without downtime, then stops on its own (no background job — the
# auth check honours the future revoked_at).
ROTATION_GRACE = timedelta(hours=1)


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(dt: datetime) -> datetime:
    # Defensive only: every mapped datetime column goes through Advanced
    # Alchemy's DateTimeUTC, which re-attaches UTC on read (SQLite included),
    # so persisted values arrive aware. This guards non-ORM callers (library
    # use, hand-built entities) from a naive-vs-aware TypeError — do not
    # "fix" other aware comparisons on the assumption reads can be naive.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class APIKeyService:
    def __init__(
        self,
        repository: APIKeyRepository,
        transaction: Transaction,
        users: UserRepository,
        service_principals: ServicePrincipalRepository | None = None,
        audit_log: AuditLog | None = None,
    ) -> None:
        self._repo = repository
        self._transaction = transaction
        self._users = users
        self._sps = service_principals
        self._audit = audit_log

    @asynccontextmanager
    async def _unit_of_work(self) -> AsyncGenerator[None]:
        try:
            yield
            await self._transaction.commit()
        except Exception:
            await self._transaction.rollback()
            raise

    async def _require_active_personal_owner(self, user_id: UUID) -> None:
        owner = await self._users.get_for_update(user_id)
        if owner is None or not owner.is_active:
            raise PermissionDenied("Personal API key owner is inactive")

    async def _require_enabled_service_principal(
        self, team_id: UUID, service_principal_id: UUID
    ) -> None:
        if self._sps is None:
            raise RuntimeError("Service-principal repository is required")
        service_principal = await self._sps.get_for_update(service_principal_id)
        if service_principal is None or service_principal.team_id != team_id:
            raise ServicePrincipalNotFound(str(service_principal_id))
        if not service_principal.enabled:
            raise PermissionDenied("Service principal is disabled")

    async def _stage_issue(
        self,
        team_id: UUID,
        created_by: UUID,
        name: str | None,
        scope: KeyScope,
        service_principal_id: UUID | None,
        rate_limit_rpm: int | None,
        expires_at: datetime | None = None,
    ) -> IssuedKey:
        material = generate_key()
        stored = await self._repo.add(
            APIKey(
                id=uuid4(),
                team_id=team_id,
                created_by=created_by,
                name=name,
                prefix=material.prefix,
                key_hash=material.key_hash,
                created_at=_now(),
                revoked_at=None,
                last_used_at=None,
                scope=scope,
                service_principal_id=service_principal_id,
                rate_limit_rpm=rate_limit_rpm,
                expires_at=expires_at,
            )
        )
        return IssuedKey(key=stored, plaintext=material.plaintext)

    async def issue(
        self,
        team_id: UUID,
        created_by: UUID,
        name: str | None = None,
        scope: KeyScope = KeyScope.INFERENCE,
        service_principal_id: UUID | None = None,
        rate_limit_rpm: int | None = None,
        expires_at: datetime | None = None,
    ) -> IssuedKey:
        # Management/all scope is reserved for service-principal keys: a personal
        # key (no SP) can only ever do inference. A human manages via their JWT.
        if scope.allows_management and service_principal_id is None:
            raise ManagementScopeRequiresServicePrincipal(
                "Only a service-principal key can hold management scope"
            )
        async with self._unit_of_work():
            if service_principal_id is None:
                await self._require_active_personal_owner(created_by)
            else:
                await self._require_enabled_service_principal(team_id, service_principal_id)
            return await self._stage_issue(
                team_id,
                created_by,
                name,
                scope,
                service_principal_id,
                rate_limit_rpm,
                expires_at,
            )

    async def authenticate(self, plaintext: str) -> APIKey:
        key = await self._repo.get_by_hash(hash_key(plaintext))
        if key is None or not key.is_active:
            raise InvalidAPIKey("Invalid or revoked API key")
        if key.service_principal_id is None:
            owner = await self._users.get(key.created_by)
            if owner is None or not owner.is_active:
                raise InvalidAPIKey("Invalid or revoked API key")
        # An SP key is only valid while its service principal is enabled:
        # disabling the SP is a kill switch for ALL its keys — inference too,
        # not just management. (Deletion revokes the keys outright.)
        if key.service_principal_id is not None:
            if self._sps is None:
                raise InvalidAPIKey("Invalid or revoked API key")
            sp = await self._sps.get(key.service_principal_id)
            if sp is None or not sp.enabled:
                raise InvalidAPIKey("Invalid or revoked API key")
        now = _now()
        # Throttle the last_used_at write: skip it (and the DB commit) if it was
        # updated recently, so the auth hot path isn't a write on every request.
        if key.last_used_at is None or now - _as_utc(key.last_used_at) >= _LAST_USED_THROTTLE:
            if not await self._repo.touch_last_used(key.id, now):
                raise InvalidAPIKey("Invalid or revoked API key")
            key = dataclasses.replace(key, last_used_at=now)
        # Telemetry writes are throttled, authorization is not. This single
        # statement is the linearization point for key + owner/SP validity.
        if not await self._repo.is_authenticatable(key.id):
            raise InvalidAPIKey("Invalid or revoked API key")
        return key

    async def revoke_for_service_principal(self, sp_id: UUID, revoked_at: datetime) -> None:
        await self._repo.revoke_for_service_principal(sp_id, revoked_at)

    async def revoke_for_team(self, team_id: UUID, key_id: UUID) -> None:
        key = await self._repo.get(key_id)
        if key is None or key.team_id != team_id:
            raise APIKeyNotFound(str(key_id))
        if key.is_active:
            await self._repo.update(dataclasses.replace(key, revoked_at=_now()))

    async def get_active_for_team(self, team_id: UUID, key_id: UUID) -> APIKey:
        """Return an active key belonging to ``team_id``, or raise not-found."""
        key = await self._repo.get(key_id)
        if key is None or key.team_id != team_id or not key.is_active:
            raise APIKeyNotFound(str(key_id))
        return key

    async def rotate_for_team(
        self,
        team_id: UUID,
        key_id: UUID,
        *,
        grace: timedelta = ROTATION_GRACE,
        audit_event: AuditEvent | None = None,
    ) -> IssuedKey:
        """Issue a replacement key (same scope, rate limit, and owner) and schedule
        the old one to stop working after `grace`, so clients can migrate without
        downtime. For an immediate cut-over (e.g. a leaked key), revoke instead."""
        snapshot = await self.get_active_for_team(team_id, key_id)
        async with self._unit_of_work():
            # Deactivation locks the same owner row before revoking keys. Taking
            # that lock first serializes personal issue/rotate with offboarding.
            if snapshot.service_principal_id is None:
                await self._require_active_personal_owner(snapshot.created_by)
            else:
                await self._require_enabled_service_principal(
                    team_id, snapshot.service_principal_id
                )
            key = await self._repo.get_for_update(key_id)
            if (
                key is None
                or key.team_id != team_id
                or key.revoked_at is not None
                or key.created_by != snapshot.created_by
                or key.service_principal_id != snapshot.service_principal_id
            ):
                raise APIKeyNotFound(str(key_id))
            issued = await self._stage_issue(
                team_id,
                key.created_by,
                key.name,
                key.scope,
                key.service_principal_id,
                key.rate_limit_rpm,
                # Rotation preserves the key's TTL (absolute): a rotated key
                # expires when the original would have, never later.
                key.expires_at,
            )
            if not await self._repo.schedule_revocation(
                key.id,
                None,
                _now() + grace,
            ):
                raise APIKeyNotFound(str(key_id))
            if audit_event is not None:
                if self._audit is None:
                    raise RuntimeError("Audit log is required for audited rotation")
                detail = f"{audit_event.detail or ''} -> new key {issued.key.id}".strip()
                await self._audit.stage(dataclasses.replace(audit_event, detail=detail))
            return issued

    async def list_for_team(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[APIKey]:
        return await self._repo.list_by_team(team_id, limit=limit, offset=offset)
