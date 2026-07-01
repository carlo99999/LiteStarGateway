"""Composition root: wires settings, persistence, services and the web layer."""

from litestar import Litestar, get
from litestar.di import NamedDependency, Provide
from litestar.openapi.config import OpenAPIConfig
from litestar.openapi.plugins import ScalarRenderPlugin, StoplightRenderPlugin, SwaggerRenderPlugin
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_test.config import Settings
from litestar_test.domain.exceptions import DomainError
from litestar_test.infrastructure.bootstrap import make_bootstrap_admin
from litestar_test.infrastructure.keyring import Keyring
from litestar_test.infrastructure.logging import build_logging_config
from litestar_test.infrastructure.persistence.database import create_database
from litestar_test.infrastructure.persistence.secret_key_repository import (
    SQLAlchemySecretKeyRepository,
)
from litestar_test.infrastructure.rotation import make_rotation_scheduler
from litestar_test.infrastructure.web.api_router.dependencies import (
    build_llm_gateway,
    provide_completion_service,
    provide_usage_repository,
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
    swagger_plugin = SwaggerRenderPlugin(version="5.18.2", path="/")
    scalar_plugin = ScalarRenderPlugin(version="1.19.5", path="/scalar")
    stoplight_plugin = StoplightRenderPlugin(version="7.7.18", path="/elements")

    def provide_keyring(db_session: NamedDependency[AsyncSession]) -> Keyring:
        # Per-purpose masters: SALT_KEY wraps credential keys, JWT_SECRET wraps JWT
        # keys. Credential ops raise SaltKeyMissing (503) if SALT_KEY is unset; the
        # data keys are created on first use (no startup bootstrap needed).
        return Keyring(
            SQLAlchemySecretKeyRepository(db_session), settings.salt_key, settings.jwt_secret
        )

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
        openapi_config=OpenAPIConfig(
            title="Litestar Gateway API",
            version="0.1.0",
            description=(
                "A gateway for LLM inference, model deployments, and API key "
                "management.\n\n"
                "**Docs viewers:**\n\n"
                "- [Swagger UI](/)\n"
                "- [Scalar](/scalar)\n"
                "- [Stoplight Elements](/elements)\n"
                "- [OpenAPI schema](/openapi.json)\n"
            ),
            # Serve the docs from the root so `/` is the landing page (plugin paths
            # are relative to this base). The schema JSON is at `/openapi.json`.
            path="/",
            render_plugins=[swagger_plugin, scalar_plugin, stoplight_plugin],
        ),
        plugins=[database.plugin],
        logging_config=build_logging_config(settings),
        on_startup=[make_bootstrap_admin(database, settings)],
        lifespan=[make_rotation_scheduler(database, settings)],
        dependencies={
            "api_key_service": Provide(provide_api_key_service, sync_to_thread=False),
            "user_service": Provide(provide_user_service, sync_to_thread=False),
            "organization_service": Provide(provide_organization_service, sync_to_thread=False),
            "team_service": Provide(provide_team_service, sync_to_thread=False),
            "model_service": Provide(provide_model_service, sync_to_thread=False),
            "credential_service": Provide(provide_credential_service, sync_to_thread=False),
            "completion_service": Provide(provide_completion_service, sync_to_thread=False),
            "usage_repository": Provide(provide_usage_repository, sync_to_thread=False),
            "llm_gateway": Provide(lambda: llm_gateway, sync_to_thread=False),
            # Envelope-encryption keyring (credentials + JWT). Built lazily;
            # raises SaltKeyMissing (503) if SALT_KEY — the master key — is unset.
            "keyring": Provide(provide_keyring, sync_to_thread=False),
        },
        exception_handlers={DomainError: domain_exception_handler},
    )


@get("/health")
async def health() -> dict:
    return {"status": "ok"}


app = create_app()
