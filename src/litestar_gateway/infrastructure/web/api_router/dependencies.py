"""Dependencies for the OpenAI-compatible inference endpoints."""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.config import Settings
from litestar_gateway.domain.ports import BudgetRepository, LLMGateway, UsageRepository
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


def provide_completion_service(
    db_session: NamedDependency[AsyncSession],
    keyring: NamedDependency[Keyring],
    llm_gateway: NamedDependency[LLMGateway],
    trace_dispatcher: NamedDependency[TraceDispatcher],
) -> CompletionService:
    return CompletionService(
        models=SQLAlchemyModelRepository(db_session),
        credentials=SQLAlchemyCredentialRepository(db_session, keyring),
        gateway=llm_gateway,
        usage=SQLAlchemyUsageRepository(db_session),
        emit_trace=trace_dispatcher.enqueue,
        budgets=SQLAlchemyBudgetRepository(db_session),
    )
