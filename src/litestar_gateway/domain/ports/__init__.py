"""Ports — interfaces the application depends on. Adapters implement these.

Most ports here (persistence, identity, locking) have exactly one production
adapter; their value is enabling fast in-memory fakes in `tests/`, not backend
swappability — there's no near-term plan for a second real implementation.
`LLMGateway` is the exception (five real provider adapters) and is where the
pattern earns its abstraction beyond testability.
"""

from __future__ import annotations

from litestar_gateway.domain.ports.api_key import APIKeyRepository
from litestar_gateway.domain.ports.audit import AuditLog
from litestar_gateway.domain.ports.budget import BudgetRepository
from litestar_gateway.domain.ports.credential import CredentialRepository
from litestar_gateway.domain.ports.identity import IdentityProvider
from litestar_gateway.domain.ports.invite import InviteRepository, PasswordResetRepository
from litestar_gateway.domain.ports.llm_gateway import LLMGateway
from litestar_gateway.domain.ports.lock import DistributedLock
from litestar_gateway.domain.ports.model import ModelRepository
from litestar_gateway.domain.ports.organization import OrganizationRepository
from litestar_gateway.domain.ports.rate_limiter import RateLimitDecision, RateLimiter
from litestar_gateway.domain.ports.routing import (
    RouterRepository,
    RoutingDecisionLog,
    RoutingDecisionLogFactory,
    RoutingRepositoryFactory,
)
from litestar_gateway.domain.ports.scim_token import ScimTokenRepository
from litestar_gateway.domain.ports.secret_key import SecretKeyRepository
from litestar_gateway.domain.ports.service_principal import ServicePrincipalRepository
from litestar_gateway.domain.ports.team import TeamMembershipRepository, TeamRepository
from litestar_gateway.domain.ports.trace import TraceSink
from litestar_gateway.domain.ports.transaction import Transaction
from litestar_gateway.domain.ports.usage import UsageRepository
from litestar_gateway.domain.ports.user import UserRepository

__all__ = [
    "APIKeyRepository",
    "AuditLog",
    "BudgetRepository",
    "CredentialRepository",
    "DistributedLock",
    "IdentityProvider",
    "InviteRepository",
    "LLMGateway",
    "ModelRepository",
    "OrganizationRepository",
    "RateLimitDecision",
    "RateLimiter",
    "PasswordResetRepository",
    "RouterRepository",
    "RoutingDecisionLog",
    "RoutingDecisionLogFactory",
    "RoutingRepositoryFactory",
    "ScimTokenRepository",
    "SecretKeyRepository",
    "ServicePrincipalRepository",
    "TeamMembershipRepository",
    "TeamRepository",
    "Transaction",
    "TraceSink",
    "UsageRepository",
    "UserRepository",
]
