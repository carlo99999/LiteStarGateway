"""Dependency wiring for the model service.

The credential repository is built without a cipher: model validation only reads
credential *metadata* (provider), never the encrypted secret values.
"""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.callable_aliases import CallableAliasResolver
from litestar_gateway.application.model_service import ModelService
from litestar_gateway.infrastructure.persistence.credential_repository import (
    SQLAlchemyCredentialRepository,
)
from litestar_gateway.infrastructure.persistence.model_repository import (
    SQLAlchemyModelRepository,
)


def provide_model_service(
    db_session: NamedDependency[AsyncSession],
    callable_resolver: NamedDependency[CallableAliasResolver],
) -> ModelService:
    models = SQLAlchemyModelRepository(db_session)
    return ModelService(
        models=models,
        credentials=SQLAlchemyCredentialRepository(db_session),
        callable_resolver=callable_resolver,
    )
