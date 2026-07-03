"""Composition root: wires settings, persistence, services and the web layer."""

from litestar import Litestar, Response, get
from litestar.di import NamedDependency, Provide
from litestar.openapi.config import OpenAPIConfig
from litestar.openapi.plugins import ScalarRenderPlugin, StoplightRenderPlugin, SwaggerRenderPlugin
from litestar.status_codes import HTTP_503_SERVICE_UNAVAILABLE
from litestar.stores.base import Store
from litestar.stores.redis import RedisStore
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.config import Settings
from litestar_gateway.domain.exceptions import DomainError
from litestar_gateway.domain.ports import IdentityProvider
from litestar_gateway.infrastructure.bootstrap import make_bootstrap_admin
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.logging import build_logging_config
from litestar_gateway.infrastructure.observability.aggregator import MetricsAggregator
from litestar_gateway.infrastructure.observability.composite import CompositeTraceSink
from litestar_gateway.infrastructure.observability.dispatcher import TraceDispatcher
from litestar_gateway.infrastructure.observability.factory import build_trace_sink
from litestar_gateway.infrastructure.observability.mlflow_metrics import make_metrics_publisher
from litestar_gateway.infrastructure.persistence.database import create_database
from litestar_gateway.infrastructure.persistence.secret_key_repository import (
    SQLAlchemySecretKeyRepository,
)
from litestar_gateway.infrastructure.rotation import make_rotation_scheduler
from litestar_gateway.infrastructure.sso.oidc import OIDCIdentityProvider
from litestar_gateway.infrastructure.usage_reconciler import make_usage_reconciler
from litestar_gateway.infrastructure.web.api_router.dependencies import (
    build_llm_gateway,
    provide_budget_repository,
    provide_completion_service,
    provide_usage_repository,
)
from litestar_gateway.infrastructure.web.api_router.router import create_api_router
from litestar_gateway.infrastructure.web.audit.controller import AuditController
from litestar_gateway.infrastructure.web.audit.dependencies import provide_audit_log
from litestar_gateway.infrastructure.web.credentials import CredentialController
from litestar_gateway.infrastructure.web.credentials.dependencies import (
    provide_credential_service,
)
from litestar_gateway.infrastructure.web.dependencies import provide_api_key_service
from litestar_gateway.infrastructure.web.exception_handlers import domain_exception_handler
from litestar_gateway.infrastructure.web.models import ModelController
from litestar_gateway.infrastructure.web.models.dependencies import provide_model_service
from litestar_gateway.infrastructure.web.organizations import OrganizationController
from litestar_gateway.infrastructure.web.organizations.dependencies import (
    provide_organization_service,
    provide_team_service,
)
from litestar_gateway.infrastructure.web.session import create_session_router
from litestar_gateway.infrastructure.web.session.sso import create_sso_router
from litestar_gateway.infrastructure.web.teams import TeamController
from litestar_gateway.infrastructure.web.users import create_users_router
from litestar_gateway.infrastructure.web.users.dependencies import provide_user_service


def create_app(
    settings: Settings | None = None,
    *,
    identity_provider: IdentityProvider | None = None,
) -> Litestar:
    settings = settings or Settings.from_env()
    database = create_database(settings)
    llm_gateway = build_llm_gateway(settings)  # shared, stateless; built once
    # Observability lives on MLflow (MLFLOW_TRACKING_URI: the compose server or
    # any external one): per-call traces via the MLflow sink, fleet-level ops
    # metrics via an in-process aggregator published to a "gateway-metrics" run.
    metrics_enabled = bool(settings.mlflow_tracking_uri and settings.mlflow_metrics_interval)
    metrics_aggregator = MetricsAggregator() if metrics_enabled else None
    trace_sink = build_trace_sink(settings)
    if metrics_aggregator is not None:
        trace_sink = CompositeTraceSink([metrics_aggregator, trace_sink])
    trace_dispatcher = TraceDispatcher(  # observability, off-path
        trace_sink,
        on_drop=metrics_aggregator.record_dropped_trace if metrics_aggregator else None,
    )
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

    route_handlers: list = [
        health,  # public liveness
        readiness,  # public readiness (DB check)
        create_api_router(database.config),  # the protected "api-endpoint" group
        create_users_router(),  # signup (public) + invites (admin JWT)
        create_session_router(),  # login (public) + /me (JWT)
        OrganizationController,  # platform-admin: orgs + team creation
        TeamController,  # team-admin: members + team-scoped API keys
        ModelController,  # team-admin: team-scoped model deployments
        CredentialController,  # platform-admin: encrypted provider credentials
        AuditController,  # platform-admin: read the audit trail
    ]
    dependencies = {
        "api_key_service": Provide(provide_api_key_service, sync_to_thread=False),
        "user_service": Provide(provide_user_service, sync_to_thread=False),
        "organization_service": Provide(provide_organization_service, sync_to_thread=False),
        "team_service": Provide(provide_team_service, sync_to_thread=False),
        "model_service": Provide(provide_model_service, sync_to_thread=False),
        "credential_service": Provide(provide_credential_service, sync_to_thread=False),
        "completion_service": Provide(provide_completion_service, sync_to_thread=False),
        "usage_repository": Provide(provide_usage_repository, sync_to_thread=False),
        "budget_repository": Provide(provide_budget_repository, sync_to_thread=False),
        "audit_log": Provide(provide_audit_log, sync_to_thread=False),
        "trace_dispatcher": Provide(lambda: trace_dispatcher, sync_to_thread=False),
        "llm_gateway": Provide(lambda: llm_gateway, sync_to_thread=False),
        "keyring": Provide(provide_keyring, sync_to_thread=False),
    }

    # SSO (OIDC) is registered only when configured (or an identity provider is
    # injected, e.g. a fake in tests), so its routes/DI are absent otherwise.
    idp = identity_provider
    if idp is None and settings.sso_enabled:
        idp = OIDCIdentityProvider(
            settings.oidc_discovery_url,  # type: ignore[arg-type]  # non-None: sso_enabled
            settings.oidc_client_id,  # type: ignore[arg-type]
            settings.oidc_client_secret,
            settings.oidc_scopes,
        )
    if idp is not None:
        route_handlers.append(create_sso_router())
        dependencies["identity_provider"] = Provide(lambda: idp, sync_to_thread=False)
        dependencies["sso_admin_groups"] = Provide(
            lambda: settings.oidc_admin_groups, sync_to_thread=False
        )
        dependencies["sso_redirect_uri"] = Provide(
            lambda: settings.oidc_redirect_uri, sync_to_thread=False
        )
        dependencies["sso_cookie_secure"] = Provide(
            lambda: settings.session_cookie_secure, sync_to_thread=False
        )

    # Public, unauthenticated when enabled — operators disable it in production
    # (OPENAPI_ENABLED=false) so the full API surface isn't exposed.
    openapi_config = (
        OpenAPIConfig(
            title="Litestar Gateway API",
            version="1.0.0",
            description=(
                "A gateway for LLM inference, model deployments, and API key "
                "management.\n\n"
                "**Docs viewers:**\n\n"
                "- [Swagger UI](/)\n"
                "- [Scalar](/scalar)\n"
                "- [Stoplight Elements](/elements)\n"
                "- [OpenAPI schema](/openapi.json)\n"
            ),
            path="/",
            render_plugins=[swagger_plugin, scalar_plugin, stoplight_plugin],
        )
        if settings.openapi_enabled
        else None
    )

    # Rate-limit stores. Default (empty dict) → Litestar creates in-memory stores
    # per name (per-process). With REDIS_URL, back them with a shared Redis store so
    # limits hold across replicas. Namespaced per limiter, one client.
    stores: dict[str, Store] = {}
    if settings.redis_url:
        redis = RedisStore.with_client(url=settings.redis_url)
        stores = {
            "rate_limit_inference": redis.with_namespace("rl_inference"),
            "rate_limit_auth": redis.with_namespace("rl_auth"),
        }

    return Litestar(
        route_handlers=route_handlers,
        openapi_config=openapi_config,
        plugins=[database.plugin],
        logging_config=build_logging_config(settings),
        on_startup=[make_bootstrap_admin(database, settings)],
        lifespan=[
            make_rotation_scheduler(database, settings),
            make_usage_reconciler(database, settings),
            trace_dispatcher.run,
            *(
                [
                    make_metrics_publisher(
                        metrics_aggregator,
                        tracking_uri=settings.mlflow_tracking_uri,  # type: ignore[arg-type]
                        experiment_name=settings.mlflow_experiment,
                        interval_seconds=settings.mlflow_metrics_interval,
                    )
                ]
                if metrics_aggregator is not None
                else []
            ),
        ],
        dependencies=dependencies,
        stores=stores,
        exception_handlers={DomainError: domain_exception_handler},
    )


@get("/health")
async def health() -> dict:
    """Liveness: the process is up. Cheap, no dependencies."""
    return {"status": "ok"}


@get("/health/ready")
async def readiness(db_session: NamedDependency[AsyncSession]) -> Response[dict]:
    """Readiness: the app can actually serve — verifies DB connectivity. Returns
    503 when a dependency is unavailable so a load balancer holds traffic back."""
    try:
        await db_session.execute(text("SELECT 1"))
    except Exception:
        return Response(
            {"status": "not_ready", "checks": {"database": "down"}},
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
        )
    return Response({"status": "ready", "checks": {"database": "ok"}})


app = create_app()
