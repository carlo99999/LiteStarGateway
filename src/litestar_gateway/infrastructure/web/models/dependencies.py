"""Dependency wiring for the model service.

The credential repository is built without a cipher: model validation only reads
credential *metadata* (provider), never the encrypted secret values.
"""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_test.application.model_service import ModelService
from litestar_test.infrastructure.persistence.credential_repository import (
    SQLAlchemyCredentialRepository,
)
from litestar_test.infrastructure.persistence.model_repository import (
    SQLAlchemyModelRepository,
)


def provide_model_service(db_session: NamedDependency[AsyncSession]) -> ModelService:
    return ModelService(
        models=SQLAlchemyModelRepository(db_session),
        credentials=SQLAlchemyCredentialRepository(db_session),
    )
