"""SSO settings — platform-admin only.

The single OIDC identity provider configured for this deployment (self-hosted,
one IdP per instance). Persisted so it can be managed from the console
instead of environment variables + a restart; the DB row takes precedence
over legacy `OIDC_*` env vars whenever it exists and is enabled. `client_secret`
is encrypted at rest and never returned by any endpoint.
"""

from __future__ import annotations

from litestar import Controller, Request, get, put
from litestar.di import NamedDependency, Provide

from litestar_gateway.application.sso_settings_service import SsoSettingsService
from litestar_gateway.domain.entities import User
from litestar_gateway.domain.ports import AuditLog
from litestar_gateway.infrastructure.web.audit.recorder import record_audit
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_admin
from litestar_gateway.infrastructure.web.sso_settings.schemas import (
    SsoSettingsResponse,
    UpsertSsoSettingsRequest,
)


class SsoSettingsController(Controller):
    path = "/platform/sso-settings"
    tags = ["sso-settings"]
    # Platform-admin only (User.is_admin) — same trust level as env vars today.
    dependencies = {"current_admin": Provide(provide_current_admin)}

    @get(
        summary="Get the current SSO settings",
        description="Returns metadata only — never the client secret. 404 if unset.",
    )
    async def get_sso_settings(
        self,
        current_admin: NamedDependency[User],
        sso_settings_service: NamedDependency[SsoSettingsService],
    ) -> SsoSettingsResponse:
        settings = await sso_settings_service.get(current_admin)
        return SsoSettingsResponse.from_entity(settings)

    @put(
        summary="Create or replace the SSO settings",
        description=(
            "Upserts the single OIDC configuration for this deployment. "
            "`client_secret` omitted keeps the current one — it is never "
            "revealed, so this replaces, it does not merge."
        ),
    )
    async def upsert_sso_settings(
        self,
        data: UpsertSsoSettingsRequest,
        current_admin: NamedDependency[User],
        sso_settings_service: NamedDependency[SsoSettingsService],
        request: Request,
        audit_log: NamedDependency[AuditLog],
    ) -> SsoSettingsResponse:
        settings = await sso_settings_service.upsert(
            current_admin,
            enabled=data.enabled,
            discovery_url=data.discovery_url,
            client_id=data.client_id,
            client_secret=data.client_secret,
            scopes=data.scopes,
            admin_groups=data.admin_groups,
            default_admin=data.default_admin,
            team_mapping=data.team_mapping,
            redirect_uri=data.redirect_uri,
        )
        await record_audit(
            audit_log,
            request,
            current_admin,
            "sso_settings.update",
            target_type="sso_settings",
            target_id=settings.id,
            detail=f"enabled={settings.enabled}",
        )
        return SsoSettingsResponse.from_entity(settings)
