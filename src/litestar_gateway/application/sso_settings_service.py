"""Application service for the SSO settings singleton (platform-admin only).

Validates the payload and delegates to the repository, which encrypts the
client secret at rest. Never handles the secret's plaintext beyond passing it
straight through to the repository for encryption.
"""

from __future__ import annotations

from collections.abc import Mapping

from litestar_gateway.domain.entities import SsoSettings, User, parse_team_mapping
from litestar_gateway.domain.exceptions import (
    InvalidSsoSettings,
    PermissionDenied,
    SSONotConfigured,
)
from litestar_gateway.domain.ports import SsoSettingsRepository


def _require_platform_admin(actor: User) -> None:
    if not actor.is_admin:
        raise PermissionDenied("Platform admin privileges required")


class SsoSettingsService:
    def __init__(self, repository: SsoSettingsRepository) -> None:
        self._repo = repository

    async def get(self, actor: User) -> SsoSettings:
        _require_platform_admin(actor)
        settings = await self._repo.get()
        if settings is None:
            raise SSONotConfigured("SSO has not been configured yet")
        return settings

    async def upsert(
        self,
        actor: User,
        *,
        enabled: bool,
        discovery_url: str | None,
        client_id: str | None,
        client_secret: str | None,
        scopes: str,
        admin_groups: tuple[str, ...],
        default_admin: bool,
        team_mapping: Mapping[str, object],
        redirect_uri: str | None,
    ) -> SsoSettings:
        _require_platform_admin(actor)
        try:
            parsed_mapping = parse_team_mapping(team_mapping)
        except ValueError as exc:
            raise InvalidSsoSettings(f"team_mapping: {exc}") from exc
        if enabled:
            if not discovery_url or not discovery_url.startswith("https://"):
                raise InvalidSsoSettings("discovery_url must be an https:// URL when enabled")
            if not client_id or not client_id.strip():
                raise InvalidSsoSettings("client_id is required when enabled")
            existing = await self._repo.get()
            already_has_secret = existing is not None and existing.has_client_secret
            if client_secret is None and not already_has_secret:
                raise InvalidSsoSettings("client_secret is required to enable SSO")
        return await self._repo.upsert(
            enabled=enabled,
            discovery_url=discovery_url,
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
            admin_groups=admin_groups,
            default_admin=default_admin,
            team_mapping=parsed_mapping,
            redirect_uri=redirect_uri,
        )
