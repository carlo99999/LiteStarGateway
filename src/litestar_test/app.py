"""Composition root: wires settings, persistence, services and the web layer."""

from litestar import Litestar, get
from litestar.di import Provide

from litestar_test.config import Settings
from litestar_test.domain.exceptions import DomainError
from litestar_test.infrastructure.bootstrap import make_bootstrap_admin
from litestar_test.infrastructure.crypto import build_cipher
from litestar_test.infrastructure.logging import build_logging_config
from litestar_test.infrastructure.persistence.database import create_database
from litestar_test.infrastructure.web.api_router.dependencies import (
    build_llm_gateway,
    provide_completion_service,
)
from litestar_test.infrastructure.web.api_router.router import create_api_router
from litestar_test.infrastructure.web.credentials import CredentialController
from litestar_test.infrastructure.web.credentials.dependencies import (
    provide_credential_service,
)
from litestar_test.infrastructure.web.dependencies import provide_api_key_service
from litestar_test.infrastructure.web.exception_handlers import domain_exception_handler
from litestar_test.infrastructure.web.models import ModelController
from litestar_test.infrastructure.web.models.dependencies import provide_model_service
from litestar_test.infrastructure.web.organizations import OrganizationController
from litestar_test.infrastructure.web.organizations.dependencies import (
    provide_organization_service,
    provide_team_service,
)
from litestar_test.infrastructure.web.session import create_session_router
from litestar_test.infrastructure.web.teams import TeamController
from litestar_test.infrastructure.web.users import create_users_router
from litestar_test.infrastructure.web.users.dependencies import provide_user_service


def create_app(settings: Settings | None = None) -> Litestar:
    settings = settings or Settings.from_env()
    database = create_database(settings)
    llm_gateway = build_llm_gateway(settings)  # shared, stateless; built once

    return Litestar(
        route_handlers=[
            health,  # public
            create_api_router(database.config),  # the protected "api-endpoint" group
            create_users_router(),  # signup (public) + invites (admin JWT)
            create_session_router(),  # login (public) + /me (JWT)
            OrganizationController,  # platform-admin: orgs + team creation
            TeamController,  # team-admin: members + team-scoped API keys
            ModelController,  # team-admin: team-scoped model deployments
            CredentialController,  # platform-admin: encrypted provider credentials
        ],
        plugins=[database.plugin],
        logging_config=build_logging_config(settings),
        on_startup=[make_bootstrap_admin(database, settings)],
        dependencies={
            "api_key_service": Provide(provide_api_key_service, sync_to_thread=False),
            "user_service": Provide(provide_user_service, sync_to_thread=False),
            "organization_service": Provide(provide_organization_service, sync_to_thread=False),
            "team_service": Provide(provide_team_service, sync_to_thread=False),
            "model_service": Provide(provide_model_service, sync_to_thread=False),
            "credential_service": Provide(provide_credential_service, sync_to_thread=False),
            "completion_service": Provide(provide_completion_service, sync_to_thread=False),
            "llm_gateway": Provide(lambda: llm_gateway, sync_to_thread=False),
            # Built lazily; raises SaltKeyMissing (503) if SALT_KEY is unset.
            "credential_cipher": Provide(
                lambda: build_cipher(settings.salt_key), sync_to_thread=False
            ),
            "jwt_secret": Provide(lambda: settings.jwt_secret, sync_to_thread=False),
        },
        exception_handlers={DomainError: domain_exception_handler},
    )


@get("/health")
async def health() -> dict:
    return {"status": "ok"}


app = create_app()
