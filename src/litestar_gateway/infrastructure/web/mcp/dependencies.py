"""Dependency wiring for the MCP services.

The allowlist is static config, so it is parsed once at app build time rather
than on every write — the same closure shape `build_credential_service_provider`
uses for the provider allowlist.
"""

from __future__ import annotations

from collections.abc import Callable

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.mcp_proposal_service import McpProposalService
from litestar_gateway.application.mcp_registry import McpRegistry
from litestar_gateway.application.mcp_service import ApiKeyToolPolicyService, McpServerService
from litestar_gateway.application.service import APIKeyService
from litestar_gateway.application.team_service import TeamService
from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.mcp.client import McpDiscoveryClient
from litestar_gateway.infrastructure.persistence.mcp_repository import (
    SQLAlchemyApiKeyToolPolicyRepository,
    SQLAlchemyMcpServerProposalRepository,
    SQLAlchemyMcpServerRepository,
)


def build_mcp_server_service_provider(
    settings: Settings,
) -> Callable[[AsyncSession, Keyring, TeamService], McpServerService]:
    allowlist = settings.mcp_allowlist()
    # One client for the app's lifetime: it holds no connection, only the
    # allowlist and the timeout, and it opens a fresh session per discovery.
    discovery = McpDiscoveryClient(allowlist=allowlist)
    ttl_seconds = settings.mcp_inventory_ttl_seconds

    def provide_mcp_server_service(
        db_session: NamedDependency[AsyncSession],
        keyring: NamedDependency[Keyring],
        team_service: NamedDependency[TeamService],
    ) -> McpServerService:
        return McpServerService(
            SQLAlchemyMcpServerRepository(db_session, keyring),
            team_service,
            allowlist=allowlist,
            discovery=discovery,
            inventory_ttl_seconds=ttl_seconds,
        )

    return provide_mcp_server_service


def build_mcp_proposal_service_provider(
    settings: Settings,
) -> Callable[[AsyncSession, Keyring, TeamService], McpProposalService]:
    """The proposal service gets a discovery client for the same reason the server
    service does — it runs the first `tools/list` at approval, which is the one
    moment §2.4 permits egress on this path. Filing never reaches it."""
    allowlist = settings.mcp_allowlist()
    discovery = McpDiscoveryClient(allowlist=allowlist)

    def provide_mcp_proposal_service(
        db_session: NamedDependency[AsyncSession],
        keyring: NamedDependency[Keyring],
        team_service: NamedDependency[TeamService],
    ) -> McpProposalService:
        # Both repositories share the one session on purpose: approval claims the
        # proposal and inserts the server in a single transaction, so a failed
        # registration cannot leave a claimed proposal behind.
        return McpProposalService(
            SQLAlchemyMcpServerProposalRepository(db_session, keyring),
            SQLAlchemyMcpServerRepository(db_session, keyring),
            team_service,
            allowlist=allowlist,
            discovery=discovery,
        )

    return provide_mcp_proposal_service


def provide_mcp_registry(
    db_session: NamedDependency[AsyncSession],
) -> McpRegistry:
    """No keyring and no discovery client: the registry reads names, urls and
    inventories, so it cannot decrypt a token and cannot cause egress even by
    mistake. That is a stronger guarantee than a code review promising it does not."""
    return McpRegistry(
        SQLAlchemyMcpServerRepository(db_session),
        SQLAlchemyApiKeyToolPolicyRepository(db_session),
    )


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
