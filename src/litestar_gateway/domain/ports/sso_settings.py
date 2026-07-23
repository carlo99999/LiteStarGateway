"""Port — SSO settings persistence (the single, DB-backed OIDC configuration
for this deployment)."""

from __future__ import annotations

from typing import Protocol

from litestar_gateway.domain.entities import SsoSettings, TeamGrant


class SsoSettingsRepository(Protocol):
    """Persistence port for the SSO settings singleton.

    The adapter encrypts `client_secret` at rest; `get()` never exposes it.
    """

    async def get(self) -> SsoSettings | None: ...

    async def get_client_secret(self) -> str | None: ...

    async def upsert(
        self,
        *,
        enabled: bool,
        discovery_url: str | None,
        client_id: str | None,
        client_secret: str | None,
        scopes: str,
        admin_groups: tuple[str, ...],
        default_admin: bool,
        team_mapping: dict[str, tuple[TeamGrant, ...]],
        redirect_uri: str | None,
    ) -> SsoSettings:
        """Create or replace the singleton row. `client_secret=None` keeps the
        existing encrypted secret (a full replacement set otherwise, mirroring
        `CredentialRepository.update`'s `values` semantics)."""
        ...
