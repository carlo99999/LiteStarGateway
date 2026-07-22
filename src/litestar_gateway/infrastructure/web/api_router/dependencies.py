"""Dependencies for the OpenAI-compatible inference endpoints."""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.callable_aliases import CallableAliasResolver
from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.application.usage_meter import InFlightSpend, UsageMeter
from litestar_gateway.config import Settings
from litestar_gateway.domain.ports import (
    BudgetRepository,
    LLMGateway,
    RateLimiter,
    RoutingDecisionLogFactory,
    RoutingRepositoryFactory,
    UsageRepository,
)
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.llm.gateway import LLMGatewayImpl
from litestar_gateway.infrastructure.llm.resilience import ResilienceConfig
from litestar_gateway.infrastructure.observability.dispatcher import TraceDispatcher
from litestar_gateway.infrastructure.persistence.budget_repository import (
    SQLAlchemyBudgetRepository,
)
from litestar_gateway.infrastructure.persistence.credential_repository import (
    SQLAlchemyCredentialRepository,
)
from litestar_gateway.infrastructure.persistence.model_repository import (
    SQLAlchemyModelRepository,
)
from litestar_gateway.infrastructure.persistence.repository import (
    SQLAlchemyAPIKeyRepository,
)
from litestar_gateway.infrastructure.persistence.router_repository import (
    SQLAlchemyRouterRepository,
    SQLAlchemyRoutingDecisionLog,
)
from litestar_gateway.infrastructure.persistence.team_repository import (
    SQLAlchemyTeamRepository,
)
from litestar_gateway.infrastructure.persistence.usage_repository import (
    SQLAlchemyUsageRepository,
)


def build_llm_gateway(settings: Settings) -> LLMGateway:
    """Build the shared gateway once, with provider-call resilience from settings.

    Stateless afterwards; adapters build provider clients per call."""
    return LLMGatewayImpl(
        ResilienceConfig(timeout=settings.request_timeout, max_retries=settings.max_retries)
    )


def provide_usage_repository(db_session: NamedDependency[AsyncSession]) -> UsageRepository:
    return SQLAlchemyUsageRepository(db_session)


def provide_budget_repository(db_session: NamedDependency[AsyncSession]) -> BudgetRepository:
    return SQLAlchemyBudgetRepository(db_session)


# Process-wide: request-scoped meters must share the in-flight reservations
# or the budget gate's burst bound would reset per request.
_in_flight_spend = InFlightSpend()


def provide_completion_service(
    db_session: NamedDependency[AsyncSession],
    keyring: NamedDependency[Keyring],
    llm_gateway: NamedDependency[LLMGateway],
    trace_dispatcher: NamedDependency[TraceDispatcher],
    shadow_decision_log_factory: NamedDependency[RoutingDecisionLogFactory],
    shadow_repos_factory: NamedDependency[RoutingRepositoryFactory],
    rate_limiter: NamedDependency[RateLimiter],
    callable_resolver: NamedDependency[CallableAliasResolver],
) -> CompletionService:
    # One request-scoped meter, shared by the completion path and the router:
    # judge/embeddings strategies make real, billable provider calls that must be
    # budget-gated and billed through the same meter as a user-facing call (H22).
    meter = UsageMeter(
        usage=SQLAlchemyUsageRepository(db_session),
        emit_trace=trace_dispatcher.enqueue,
        budgets=SQLAlchemyBudgetRepository(db_session),
        in_flight=_in_flight_spend,
        rate_limiter=rate_limiter,
        teams=SQLAlchemyTeamRepository(db_session),
        api_keys=SQLAlchemyAPIKeyRepository(db_session),
    )
    models = SQLAlchemyModelRepository(db_session)
    routers = SQLAlchemyRouterRepository(db_session, keyring)
    router_service = RouterService(
        routers=routers,
        models=models,
        decisions=SQLAlchemyRoutingDecisionLog(db_session),
        shadow_decisions=shadow_decision_log_factory,
        credentials=SQLAlchemyCredentialRepository(db_session, keyring),
        gateway=llm_gateway,
        shadow_repos=shadow_repos_factory,
        meter=meter,
        callable_resolver=callable_resolver,
    )
    return CompletionService(
        models=models,
        credentials=SQLAlchemyCredentialRepository(db_session, keyring),
        gateway=llm_gateway,
        router_service=router_service,
        meter=meter,
        callable_resolver=callable_resolver,
    )
