"""Resolves the identity provider to use for a given SSO request.

The gateway is self-hosted per customer (one IdP per instance), but the
config can live in two places: the DB-backed `sso_settings` singleton (UI
managed, hot-reloadable) or legacy `OIDC_*` env vars (frozen at process
boot). The DB wins whenever a row exists and is enabled; env vars remain a
zero-migration fallback for existing deployments. Explicit override (tests
injecting a fake `IdentityProvider` into `create_app()`) always wins over
both and skips the DB lookup entirely.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.config import Settings
from litestar_gateway.domain.entities import TeamGrant
from litestar_gateway.domain.exceptions import SSONotConfigured
from litestar_gateway.domain.ports import IdentityProvider
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.sso_settings_repository import (
    SQLAlchemySsoSettingsRepository,
)
from litestar_gateway.infrastructure.sso.oidc import OIDCIdentityProvider


@dataclass(frozen=True)
class ResolvedSsoConfig:
    """Everything `/sso/login` and `/sso/callback` need for this request,
    read as one consistent snapshot instead of five independently-sourced
    values."""

    identity_provider: IdentityProvider
    admin_groups: tuple[str, ...]
    default_admin: bool
    team_mapping: dict[str, tuple[TeamGrant, ...]]
    redirect_uri: str | None


class SsoIdentityProviderCache:
    """Rebuilds the wrapped `OIDCIdentityProvider` only when its config
    actually changes, so requests that don't touch the admin settings keep
    its discovery-document/JWKS cache warm instead of refetching the IdP on
    every single login."""

    def __init__(self) -> None:
        self._fingerprint: tuple[str, str, str, str] | None = None
        self._provider: OIDCIdentityProvider | None = None

    def resolve(
        self, discovery_url: str, client_id: str, client_secret: str, scopes: str
    ) -> OIDCIdentityProvider:
        fingerprint = (discovery_url, client_id, client_secret, scopes)
        if fingerprint != self._fingerprint or self._provider is None:
            self._provider = OIDCIdentityProvider(discovery_url, client_id, client_secret, scopes)
            self._fingerprint = fingerprint
        return self._provider


def build_sso_config_provider(
    settings: Settings,
    env_identity_provider: IdentityProvider | None,
    *,
    explicit_override: bool,
) -> Callable[..., Awaitable[ResolvedSsoConfig]]:
    """Build the per-request dependency-provider function for `sso_config`.
    `idp_cache` lives for the process lifetime, closed over here exactly like
    `llm_gateway`/`rate_limiter` in `app.py`."""
    idp_cache = SsoIdentityProviderCache()

    def _env_config() -> ResolvedSsoConfig | None:
        if env_identity_provider is None:
            return None
        return ResolvedSsoConfig(
            identity_provider=env_identity_provider,
            admin_groups=settings.oidc_admin_groups,
            default_admin=settings.default_admin,
            team_mapping=settings.oidc_team_mapping,
            redirect_uri=settings.oidc_redirect_uri,
        )

    async def provide_current_sso_config(
        db_session: NamedDependency[AsyncSession],
        keyring: NamedDependency[Keyring],
    ) -> ResolvedSsoConfig:
        if not explicit_override:
            repo = SQLAlchemySsoSettingsRepository(db_session, keyring)
            db_settings = await repo.get()
            if db_settings is not None and db_settings.enabled:
                secret = await repo.get_client_secret()
                if secret and db_settings.discovery_url and db_settings.client_id:
                    idp = idp_cache.resolve(
                        db_settings.discovery_url,
                        db_settings.client_id,
                        secret,
                        db_settings.scopes,
                    )
                    return ResolvedSsoConfig(
                        identity_provider=idp,
                        admin_groups=db_settings.admin_groups,
                        default_admin=db_settings.default_admin,
                        team_mapping=db_settings.team_mapping,
                        redirect_uri=db_settings.redirect_uri,
                    )
        env_config = _env_config()
        if env_config is not None:
            return env_config
        raise SSONotConfigured("No SSO identity provider is configured")

    return provide_current_sso_config
