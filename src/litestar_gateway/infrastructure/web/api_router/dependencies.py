"""Dependencies for the OpenAI-compatible inference endpoints."""

from __future__ import annotations

from uuid import UUID

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.callable_aliases import CallableAliasResolver
from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.application.guardrails.factory import build_chain
from litestar_gateway.application.guardrails.judge_call import judge_completer
from litestar_gateway.application.guardrails.service import ChainedProvider
from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.config import Settings
from litestar_gateway.domain.entities import Model
from litestar_gateway.domain.guardrails import Direction
from litestar_gateway.domain.ports import (
    ApiKeyBudgetRepository,
    BudgetAlertStateRepository,
    BudgetRepository,
    BudgetReservationStore,
    CircuitBreaker,
    LLMGateway,
    RateLimiter,
    ResponseCache,
    RoutingDecisionLogFactory,
    RoutingRepositoryFactory,
    SemanticResponseCache,
    UsageRepository,
)
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.llm.gateway import LLMGatewayImpl
from litestar_gateway.infrastructure.llm.resilience import ResilienceConfig
from litestar_gateway.infrastructure.observability.dispatcher import TraceDispatcher
from litestar_gateway.infrastructure.persistence.api_key_budget_repository import (
    SQLAlchemyApiKeyBudgetRepository,
)
from litestar_gateway.infrastructure.persistence.budget_alert_state_repository import (
    SQLAlchemyBudgetAlertStateRepository,
)
from litestar_gateway.infrastructure.persistence.budget_repository import (
    SQLAlchemyBudgetRepository,
)
from litestar_gateway.infrastructure.persistence.credential_repository import (
    SQLAlchemyCredentialRepository,
)
from litestar_gateway.infrastructure.persistence.guardrail_repository import (
    SQLAlchemyGuardrailRuleRepository,
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


def build_llm_gateway(settings: Settings) -> LLMGatewayImpl:
    """Build the shared gateway once, with provider-call resilience from settings.

    Returns the concrete type (not the `LLMGateway` port) so the composition
    root can call `aclose()` on it at shutdown; callers that only need the
    port's request-serving surface can still assign it to an `LLMGateway`-typed
    variable, since `LLMGatewayImpl` satisfies that Protocol structurally."""
    return LLMGatewayImpl(
        ResilienceConfig(timeout=settings.request_timeout, max_retries=settings.max_retries),
        # Static config, parsed once here rather than per call: the same
        # allowlist the credential write path uses, so a target cannot be
        # authorized by one and not the other.
        egress_allowlist=settings.egress_allowlist(),
    )


def provide_usage_repository(db_session: NamedDependency[AsyncSession]) -> UsageRepository:
    return SQLAlchemyUsageRepository(db_session)


def provide_budget_repository(
    db_session: NamedDependency[AsyncSession],
    keyring: NamedDependency[Keyring],
) -> BudgetRepository:
    # The keyring is only used for the per-team webhook secret; the budget gate
    # itself never touches it.
    return SQLAlchemyBudgetRepository(db_session, keyring)


def provide_api_key_budget_repository(
    db_session: NamedDependency[AsyncSession],
) -> ApiKeyBudgetRepository:
    return SQLAlchemyApiKeyBudgetRepository(db_session)


def provide_budget_alert_state_repository(
    db_session: NamedDependency[AsyncSession],
) -> BudgetAlertStateRepository:
    return SQLAlchemyBudgetAlertStateRepository(db_session)


def provide_completion_service(
    db_session: NamedDependency[AsyncSession],
    keyring: NamedDependency[Keyring],
    llm_gateway: NamedDependency[LLMGateway],
    trace_dispatcher: NamedDependency[TraceDispatcher],
    shadow_decision_log_factory: NamedDependency[RoutingDecisionLogFactory],
    shadow_repos_factory: NamedDependency[RoutingRepositoryFactory],
    rate_limiter: NamedDependency[RateLimiter],
    callable_resolver: NamedDependency[CallableAliasResolver],
    circuit_breaker: NamedDependency[CircuitBreaker],
    response_cache: NamedDependency[ResponseCache | None],
    response_cache_ttl_s: NamedDependency[int],
    semantic_cache: NamedDependency[SemanticResponseCache | None],
    semantic_threshold: NamedDependency[float],
    semantic_embedding_model: NamedDependency[str | None],
    budget_reservations: NamedDependency[BudgetReservationStore],
    reservation_ttl_s: NamedDependency[int],
) -> CompletionService:
    # One request-scoped meter, shared by the completion path and the router:
    # judge/embeddings strategies make real, billable provider calls that must be
    # budget-gated and billed through the same meter as a user-facing call (H22).
    meter = UsageMeter(
        usage=SQLAlchemyUsageRepository(db_session),
        emit_trace=trace_dispatcher.enqueue,
        budgets=SQLAlchemyBudgetRepository(db_session),
        reservations=budget_reservations,
        reservation_ttl_s=reservation_ttl_s,
        rate_limiter=rate_limiter,
        teams=SQLAlchemyTeamRepository(db_session),
        api_keys=SQLAlchemyAPIKeyRepository(db_session),
        budget_alert_state=SQLAlchemyBudgetAlertStateRepository(db_session),
        api_key_budgets=SQLAlchemyApiKeyBudgetRepository(db_session),
    )
    models = SQLAlchemyModelRepository(db_session)
    credentials = SQLAlchemyCredentialRepository(db_session, keyring)
    guardrail_rules = SQLAlchemyGuardrailRuleRepository(db_session, keyring)

    async def guardrails(
        team_id: UUID,
        api_key_id: UUID | None,
        model: Model,
        direction: Direction,
        router_id: UUID | None = None,
    ) -> tuple[ChainedProvider, ...]:
        """Resolve and instantiate the team's chain for this call.

        Rules are read per request rather than cached: a chain is a safety
        control, and an operator who disables one expects the next request to
        respect that, not the one after the cache expires. The read is a single
        indexed query on a table with a handful of rows per team.
        """
        active = await guardrail_rules.resolve(team_id, model.id, direction, router_id)
        if not active:
            return ()
        return build_chain(
            active,
            complete=judge_completer(
                models=models,
                credentials=credentials,
                gateway=llm_gateway,
                meter=meter,
                team_id=team_id,
                api_key_id=api_key_id,
            ),
        )

    routers = SQLAlchemyRouterRepository(db_session, keyring)
    router_service = RouterService(
        routers=routers,
        models=models,
        decisions=SQLAlchemyRoutingDecisionLog(db_session),
        shadow_decisions=shadow_decision_log_factory,
        credentials=credentials,
        gateway=llm_gateway,
        shadow_repos=shadow_repos_factory,
        meter=meter,
        callable_resolver=callable_resolver,
    )
    return CompletionService(
        models=models,
        credentials=credentials,
        gateway=llm_gateway,
        router_service=router_service,
        meter=meter,
        callable_resolver=callable_resolver,
        circuit_breaker=circuit_breaker,
        response_cache=response_cache,
        response_cache_ttl_s=response_cache_ttl_s,
        semantic_cache=semantic_cache,
        semantic_threshold=semantic_threshold,
        semantic_embedding_model=semantic_embedding_model,
        guardrails=guardrails,
    )
