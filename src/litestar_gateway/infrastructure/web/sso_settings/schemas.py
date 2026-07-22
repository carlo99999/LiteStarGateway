"""DTOs for the SSO settings singleton (OpenAPI-documented)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated
from uuid import UUID

from litestar.params import Parameter

from litestar_gateway.config import DEFAULT_OIDC_SCOPES
from litestar_gateway.domain.entities import SsoSettings, team_mapping_to_json


@dataclass(frozen=True)
class UpsertSsoSettingsRequest:
    """Create or replace the SSO settings singleton. `client_secret` omitted
    keeps the existing encrypted secret (never revealed by any endpoint, so
    this is a full replacement set, not a merge)."""

    enabled: Annotated[bool, Parameter(description="Whether SSO login is active.")]
    discovery_url: Annotated[
        str | None,
        Parameter(description="The IdP's `.well-known/openid-configuration` URL."),
    ] = None
    client_id: Annotated[str | None, Parameter(description="OIDC client id.")] = None
    client_secret: Annotated[
        str | None,
        Parameter(
            description=(
                "OIDC client secret. Omit to keep the current one — it is never "
                "returned by any endpoint."
            )
        ),
    ] = None
    scopes: Annotated[str, Parameter(description="Space-separated OIDC scopes.")] = (
        DEFAULT_OIDC_SCOPES
    )
    admin_groups: Annotated[
        tuple[str, ...], Parameter(description="IdP groups mapped to platform admin.")
    ] = ()
    default_admin: Annotated[
        bool,
        Parameter(description="Platform role a brand-new SSO user gets (admin vs member)."),
    ] = False
    team_mapping: Annotated[
        dict[str, list[dict[str, str]]],
        Parameter(description="IdP group -> [{'team': <uuid>, 'role': 'admin'|'member'}, ...]."),
    ] = field(default_factory=dict)
    redirect_uri: Annotated[
        str | None,
        Parameter(description="Public callback URL, e.g. behind a reverse proxy."),
    ] = None


@dataclass(frozen=True)
class SsoSettingsResponse:
    """SSO settings metadata. `client_secret` is intentionally never included —
    `has_client_secret` only says whether one is stored."""

    id: UUID
    enabled: bool
    discovery_url: str | None
    client_id: str | None
    scopes: str
    admin_groups: tuple[str, ...]
    default_admin: bool
    team_mapping: dict[str, list[dict[str, str]]]
    redirect_uri: str | None
    has_client_secret: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, settings: SsoSettings) -> SsoSettingsResponse:
        return cls(
            id=settings.id,
            enabled=settings.enabled,
            discovery_url=settings.discovery_url,
            client_id=settings.client_id,
            scopes=settings.scopes,
            admin_groups=settings.admin_groups,
            default_admin=settings.default_admin,
            team_mapping=team_mapping_to_json(settings.team_mapping),
            redirect_uri=settings.redirect_uri,
            has_client_secret=settings.has_client_secret,
            created_at=settings.created_at,
            updated_at=settings.updated_at,
        )
