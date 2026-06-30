"""Dependencies for the OpenAI-compatible inference endpoints."""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_test.application.completion_service import CompletionService
from litestar_test.domain.ports import LLMGateway
from litestar_test.infrastructure.crypto import CredentialCipher
from litestar_test.infrastructure.llm.gateway import LLMGatewayImpl
from litestar_test.infrastructure.persistence.credential_repository import (
    SQLAlchemyCredentialRepository,
)
from litestar_test.infrastructure.persistence.model_repository import (
    SQLAlchemyModelRepository,
)

# Stateless; adapters build provider clients per call.
_GATEWAY = LLMGatewayImpl()


def provide_llm_gateway() -> LLMGateway:
    return _GATEWAY


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
