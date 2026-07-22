"""Request-scoped wiring for the shared callable resolver."""

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.callable_aliases import CallableAliasResolver
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.callable_alias_repository import (
    SQLAlchemyCallableAliasRepository,
)
from litestar_gateway.infrastructure.persistence.model_repository import SQLAlchemyModelRepository
from litestar_gateway.infrastructure.persistence.router_repository import SQLAlchemyRouterRepository


def provide_callable_resolver(
    db_session: NamedDependency[AsyncSession], keyring: NamedDependency[Keyring]
) -> CallableAliasResolver:
    return CallableAliasResolver(
        SQLAlchemyCallableAliasRepository(db_session),
        SQLAlchemyModelRepository(db_session),
        SQLAlchemyRouterRepository(db_session, keyring),
    )
