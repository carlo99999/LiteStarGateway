"""Application service — orchestrates the API key use cases.

Depends only on the `APIKeyRepository` port and pure domain logic, never on
SQLAlchemy or Litestar. This is the hexagon's core.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from litestar_gateway.domain.entities import APIKey, IssuedKey, KeyScope
from litestar_gateway.domain.exceptions import (
    APIKeyNotFound,
    InvalidAPIKey,
    ManagementScopeRequiresServicePrincipal,
)
from litestar_gateway.domain.key_generator import generate_key, hash_key
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.domain.ports import APIKeyRepository, ServicePrincipalRepository

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
        service_principals: ServicePrincipalRepository | None = None,
    ) -> None:
        self._repo = repository
        # Optional: only the auth path needs it (to enforce SP.enabled). Callers
        # that merely issue/revoke keys leave it None.
        self._sps = service_principals

    async def issue(
        self,
        team_id: UUID,
        created_by: UUID,
        name: str | None = None,
        scope: KeyScope = KeyScope.INFERENCE,
        service_principal_id: UUID | None = None,
        rate_limit_rpm: int | None = None,
    ) -> IssuedKey:
        # Management/all scope is reserved for service-principal keys: a personal
        # key (no SP) can only ever do inference. A human manages via their JWT.
        if scope.allows_management and service_principal_id is None:
            raise ManagementScopeRequiresServicePrincipal(
                "Only a service-principal key can hold management scope"
            )
        material = generate_key()
        key = APIKey(
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
        )
        stored = await self._repo.add(key)
        return IssuedKey(key=stored, plaintext=material.plaintext)

    async def authenticate(self, plaintext: str) -> APIKey:
        key = await self._repo.get_by_hash(hash_key(plaintext))
        if key is None or not key.is_active:
            raise InvalidAPIKey("Invalid or revoked API key")
        # An SP key is only valid while its service principal is enabled:
        # disabling the SP is a kill switch for ALL its keys — inference too,
        # not just management. (Deletion revokes the keys outright.)
        if key.service_principal_id is not None and self._sps is not None:
            sp = await self._sps.get(key.service_principal_id)
            if sp is None or not sp.enabled:
                raise InvalidAPIKey("Invalid or revoked API key")
        now = _now()
        # Throttle the last_used_at write: skip it (and the DB commit) if it was
        # updated recently, so the auth hot path isn't a write on every request.
        if key.last_used_at is None or now - _as_utc(key.last_used_at) >= _LAST_USED_THROTTLE:
            return await self._repo.update(dataclasses.replace(key, last_used_at=now))
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
        created_by: UUID,
        *,
        grace: timedelta = ROTATION_GRACE,
    ) -> IssuedKey:
        """Issue a replacement key (same scope, rate limit, and owner) and schedule
        the old one to stop working after `grace`, so clients can migrate without
        downtime. For an immediate cut-over (e.g. a leaked key), revoke instead."""
        key = await self.get_active_for_team(team_id, key_id)
        issued = await self.issue(
            team_id=team_id,
            created_by=created_by,
            name=key.name,
            scope=key.scope,
            service_principal_id=key.service_principal_id,
            rate_limit_rpm=key.rate_limit_rpm,
        )
        await self._repo.update(dataclasses.replace(key, revoked_at=_now() + grace))
        return issued

    async def list_for_team(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[APIKey]:
        return await self._repo.list_by_team(team_id, limit=limit, offset=offset)
