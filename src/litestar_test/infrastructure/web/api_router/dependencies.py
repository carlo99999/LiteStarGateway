"""Dependencies for the OpenAI-compatible inference endpoints."""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_test.application.completion_service import CompletionService
from litestar_test.config import Settings
from litestar_test.domain.ports import LLMGateway
from litestar_test.infrastructure.crypto import CredentialCipher
from litestar_test.infrastructure.llm.gateway import LLMGatewayImpl
from litestar_test.infrastructure.llm.resilience import ResilienceConfig
from litestar_test.infrastructure.persistence.credential_repository import (
    SQLAlchemyCredentialRepository,
)
from litestar_test.infrastructure.persistence.model_repository import (
    SQLAlchemyModelRepository,
)


def build_llm_gateway(settings: Settings) -> LLMGateway:
    """Build the shared gateway once, with provider-call resilience from settings.

    Stateless afterwards; adapters build provider clients per call."""
    return LLMGatewayImpl(
        ResilienceConfig(timeout=settings.request_timeout, max_retries=settings.max_retries)
    )


def provide_completion_service(
    db_session: NamedDependency[AsyncSession],
    credential_cipher: NamedDependency[CredentialCipher],
    llm_gateway: NamedDependency[LLMGateway],
) -> CompletionService:
    return CompletionService(
        models=SQLAlchemyModelRepository(db_session),
        credentials=SQLAlchemyCredentialRepository(db_session, credential_cipher),
        gateway=llm_gateway,
    )
