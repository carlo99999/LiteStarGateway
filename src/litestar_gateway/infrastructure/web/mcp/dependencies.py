"""Dependency wiring for the MCP services.

The allowlist is static config, so it is parsed once at app build time rather
than on every write — the same closure shape `build_credential_service_provider`
uses for the provider allowlist.
"""

from __future__ import annotations

from collections.abc import Callable

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.mcp_service import ApiKeyToolPolicyService, McpServerService
from litestar_gateway.application.service import APIKeyService
from litestar_gateway.application.team_service import TeamService
from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.mcp_repository import (
    SQLAlchemyApiKeyToolPolicyRepository,
    SQLAlchemyMcpServerRepository,
)


def build_mcp_server_service_provider(
    settings: Settings,
) -> Callable[[AsyncSession, Keyring, TeamService], McpServerService]:
    allowlist = settings.mcp_allowlist()

    def provide_mcp_server_service(
        db_session: NamedDependency[AsyncSession],
        keyring: NamedDependency[Keyring],
        team_service: NamedDependency[TeamService],
    ) -> McpServerService:
        return McpServerService(
            SQLAlchemyMcpServerRepository(db_session, keyring),
            team_service,
            allowlist=allowlist,
        )

    return provide_mcp_server_service


def provide_api_key_tool_policy_service(
    db_session: NamedDependency[AsyncSession],
    team_service: NamedDependency[TeamService],
    api_key_service: NamedDependency[APIKeyService],
) -> ApiKeyToolPolicyService:
    return ApiKeyToolPolicyService(
        SQLAlchemyApiKeyToolPolicyRepository(db_session),
        team_service,
        api_key_service,
    )
