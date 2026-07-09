"""Composition root: wires settings, persistence, services and the web layer."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from litestar import Litestar, Request, Response, get
from litestar.di import NamedDependency, Provide
from litestar.openapi.config import OpenAPIConfig
from litestar.openapi.plugins import ScalarRenderPlugin, StoplightRenderPlugin, SwaggerRenderPlugin
from litestar.status_codes import HTTP_503_SERVICE_UNAVAILABLE
from litestar.stores.base import Store
from litestar.stores.redis import RedisStore
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.routing.service import drain_shadow_tasks
from litestar_gateway.config import Settings
from litestar_gateway.domain.exceptions import DomainError
from litestar_gateway.domain.ports import IdentityProvider, LLMGateway
from litestar_gateway.infrastructure.bootstrap import make_bootstrap_admin
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.logging import build_logging_config
from litestar_gateway.infrastructure.observability.aggregator import MetricsAggregator
from litestar_gateway.infrastructure.observability.composite import CompositeTraceSink
from litestar_gateway.infrastructure.observability.dispatcher import TraceDispatcher
from litestar_gateway.infrastructure.observability.factory import build_trace_sink
from litestar_gateway.infrastructure.observability.mlflow_metrics import make_metrics_publisher
from litestar_gateway.infrastructure.persistence.database import Database, create_database
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
from litestar_gateway.infrastructure.web.docs_site import create_docs_router
from litestar_gateway.infrastructure.web.exception_handlers import domain_exception_handler
from litestar_gateway.infrastructure.web.models import ModelController
from litestar_gateway.infrastructure.web.models.dependencies import provide_model_service
from litestar_gateway.infrastructure.web.organizations import OrganizationController
from litestar_gateway.infrastructure.web.organizations.dependencies import (
    provide_organization_service,
    provide_team_service,
)
from litestar_gateway.infrastructure.web.routing import RouterController
from litestar_gateway.infrastructure.web.routing.dependencies import (
    make_shadow_log_factory,
    make_shadow_repos_factory,
    provide_router_service,
)
from litestar_gateway.infrastructure.web.scim import (
    create_scim_router,
    create_scim_tokens_router,
)
from litestar_gateway.infrastructure.web.scim.dependencies import provide_scim_service
from litestar_gateway.infrastructure.web.service_principals import ServicePrincipalController
from litestar_gateway.infrastructure.web.service_principals.dependencies import (
    provide_service_principal_service,
)
from litestar_gateway.infrastructure.web.session import create_session_router
from litestar_gateway.infrastructure.web.session.sso import create_sso_router
from litestar_gateway.infrastructure.web.teams import TeamController
from litestar_gateway.infrastructure.web.users import create_users_router
from litestar_gateway.infrastructure.web.users.dependencies import provide_user_service

logger = logging.getLogger("litestar_gateway.app")


def create_app(
    settings: Settings | None = None,
    *,
    identity_provider: IdentityProvider | None = None,
) -> Litestar:
    settings = settings or Settings.from_env()
    database = create_database(settings)
    llm_gateway = build_llm_gateway(settings)  # shared, stateless; built once
    metrics_aggregator, trace_dispatcher = _build_observability(settings)

    route_handlers = _build_route_handlers(database)
    dependencies = _build_dependencies(settings, database, trace_dispatcher, llm_gateway)

    docs_router = create_docs_router()  # built MkDocs site at /docs, if present
    if docs_router is not None:
        route_handlers.append(docs_router)

    idp = _resolve_identity_provider(settings, identity_provider)
    if idp is not None:
        route_handlers.append(create_sso_router())
        dependencies.update(_build_sso_dependencies(settings, idp))

    app = Litestar(
        route_handlers=route_handlers,
        openapi_config=_build_openapi_config(settings),
        plugins=[database.plugin],
        logging_config=build_logging_config(settings),
        on_startup=[make_bootstrap_admin(database, settings)],
        lifespan=_build_lifespan(database, settings, trace_dispatcher, metrics_aggregator),
        dependencies=dependencies,
        stores=_build_rate_limit_stores(settings),
        exception_handlers={DomainError: domain_exception_handler},
    )
    _register_shadow_drain(app)
    return app


@asynccontextmanager
async def _shadow_drain_lifespan(_app: Litestar) -> AsyncGenerator[None]:
    """Await in-flight shadow-routing tasks on shutdown before the SQLAlchemy
    plugin disposes the engine (R7-M51), so their unit of work settles instead
    of racing engine teardown."""
    try:
        yield
    finally:
        await drain_shadow_tasks()


def _register_shadow_drain(app: Litestar) -> None:
    # Ordering matters: the SQLAlchemy plugin appends its engine-disposal
    # lifespan during `on_app_init` (always last, via its init sub-plugin), and
    # Litestar unwinds lifespans LIFO. A lifespan passed to the constructor would
    # therefore tear down *after* the engine is gone — too late. Appending here,
    # after construction, makes the drain the last manager entered and so the
    # first to unwind, i.e. before the engine is disposed.
    app._lifespan_managers.append(_shadow_drain_lifespan)


def _build_observability(
    settings: Settings,
) -> tuple[MetricsAggregator | None, TraceDispatcher]:
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
    return metrics_aggregator, trace_dispatcher


def _make_shadow_log_provider(session_maker_state_key: str):
    def provide_shadow_log_factory(request: Request):
        # The session maker lands in app.state under the AA config's (possibly
        # suffixed) key — same trick as the auth middleware, read lazily.
        return make_shadow_log_factory(request.app.state[session_maker_state_key])

    return provide_shadow_log_factory


def _make_keyring_provider(settings: Settings):
    def provide_keyring(db_session: NamedDependency[AsyncSession]) -> Keyring:
        # Per-purpose masters: SALT_KEY wraps credential keys, JWT_SECRET wraps JWT
        # keys. Credential ops raise SaltKeyMissing (503) if SALT_KEY is unset; the
        # data keys are created on first use (no startup bootstrap needed).
        return Keyring(
            SQLAlchemySecretKeyRepository(db_session), settings.salt_key, settings.jwt_secret
        )

    return provide_keyring


def _make_shadow_repos_provider(session_maker_state_key: str, keyring_provider):
    def provide_shadow_repos_factory(request: Request):
        # Same own-session care as the shadow decision log: the shadow
        # strategy's model/credential lookups race the request coroutine,
        # which is still using the request-scoped session.
        return make_shadow_repos_factory(
            request.app.state[session_maker_state_key],
            keyring_provider,
        )

    return provide_shadow_repos_factory


def _build_route_handlers(database: Database) -> list:
    return [
        health,  # public liveness
        readiness,  # public readiness (DB check)
        create_api_router(database.config),  # the protected "api-endpoint" group
        create_users_router(),  # signup (public) + invites (admin JWT)
        create_session_router(),  # login (public) + /me (JWT)
        OrganizationController,  # platform-admin: orgs + team creation
        TeamController,  # team-admin: members + team-scoped API keys
        ModelController,  # team-admin: team-scoped model deployments
        RouterController,  # team-admin: smart routers (virtual models)
        ServicePrincipalController,  # team-admin: service principals + their keys
        CredentialController,  # platform-admin: encrypted provider credentials
        AuditController,  # platform-admin: read the audit trail
        create_scim_router(),  # IdP-facing SCIM 2.0 Users (provisioning-token auth)
        create_scim_tokens_router(),  # platform-admin: mint/revoke SCIM tokens
    ]


def _build_dependencies(
    settings: Settings,
    database: Database,
    trace_dispatcher: TraceDispatcher,
    llm_gateway: LLMGateway,
) -> dict[str, Provide]:
    session_maker_key = database.config.session_maker_app_state_key
    keyring_provider = _make_keyring_provider(settings)
    return {
        "api_key_service": Provide(provide_api_key_service, sync_to_thread=False),
        "user_service": Provide(provide_user_service, sync_to_thread=False),
        "organization_service": Provide(provide_organization_service, sync_to_thread=False),
        "team_service": Provide(provide_team_service, sync_to_thread=False),
        "model_service": Provide(provide_model_service, sync_to_thread=False),
        "credential_service": Provide(provide_credential_service, sync_to_thread=False),
        "completion_service": Provide(provide_completion_service, sync_to_thread=False),
        "service_principal_service": Provide(
            provide_service_principal_service, sync_to_thread=False
        ),
        "scim_service": Provide(provide_scim_service, sync_to_thread=False),
        "router_service": Provide(provide_router_service, sync_to_thread=False),
        "shadow_decision_log_factory": Provide(
            _make_shadow_log_provider(session_maker_key),
            sync_to_thread=False,
        ),
        "shadow_repos_factory": Provide(
            _make_shadow_repos_provider(session_maker_key, keyring_provider),
            sync_to_thread=False,
        ),
        "usage_repository": Provide(provide_usage_repository, sync_to_thread=False),
        "budget_repository": Provide(provide_budget_repository, sync_to_thread=False),
        "audit_log": Provide(provide_audit_log, sync_to_thread=False),
        "trace_dispatcher": Provide(lambda: trace_dispatcher, sync_to_thread=False),
        "llm_gateway": Provide(lambda: llm_gateway, sync_to_thread=False),
        "keyring": Provide(keyring_provider, sync_to_thread=False),
    }


def _resolve_identity_provider(
    settings: Settings, identity_provider: IdentityProvider | None
) -> IdentityProvider | None:
    # SSO (OIDC) is registered only when configured (or an identity provider is
    # injected, e.g. a fake in tests), so its routes/DI are absent otherwise.
    if identity_provider is None and settings.sso_enabled:
        return OIDCIdentityProvider(
            settings.oidc_discovery_url,  # type: ignore[arg-type]  # non-None: sso_enabled
            settings.oidc_client_id,  # type: ignore[arg-type]
            settings.oidc_client_secret,
            settings.oidc_scopes,
        )
    return identity_provider


def _build_sso_dependencies(settings: Settings, idp: IdentityProvider) -> dict[str, Provide]:
    return {
        "identity_provider": Provide(lambda: idp, sync_to_thread=False),
        "sso_admin_groups": Provide(lambda: settings.oidc_admin_groups, sync_to_thread=False),
        "sso_default_admin": Provide(lambda: settings.default_admin, sync_to_thread=False),
        "sso_team_mapping": Provide(lambda: settings.oidc_team_mapping, sync_to_thread=False),
        "sso_redirect_uri": Provide(lambda: settings.oidc_redirect_uri, sync_to_thread=False),
        "sso_cookie_secure": Provide(lambda: settings.session_cookie_secure, sync_to_thread=False),
    }


def _build_openapi_config(settings: Settings) -> OpenAPIConfig | None:
    # Public, unauthenticated when enabled — operators disable it in production
    # (OPENAPI_ENABLED=false) so the full API surface isn't exposed.
    if not settings.openapi_enabled:
        return None
    return OpenAPIConfig(
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
        render_plugins=[
            SwaggerRenderPlugin(version="5.18.2", path="/"),
            ScalarRenderPlugin(version="1.19.5", path="/scalar"),
            StoplightRenderPlugin(version="7.7.18", path="/elements"),
        ],
    )


def _build_rate_limit_stores(settings: Settings) -> dict[str, Store]:
    # Rate-limit stores. Default (empty dict) → Litestar creates in-memory stores
    # per name (per-process). With REDIS_URL, back them with a shared Redis store so
    # limits hold across replicas. Namespaced per limiter, one client.
    stores: dict[str, Store] = {}
    if settings.is_production and not settings.redis_url:
        # A single instance is legitimate, so this is a warning, not an error -
        # but it must be impossible to miss: with N workers/replicas every
        # configured limit silently becomes N x the intended one.
        logger.warning(
            "REDIS_URL is not set: rate limits are enforced in-memory PER "
            "PROCESS. With multiple workers or replicas every limit is "
            "effectively multiplied by the instance count - set REDIS_URL to "
            "share the stores."
        )
    if settings.redis_url:
        redis = RedisStore.with_client(url=settings.redis_url)
        stores = {
            "rate_limit_inference": redis.with_namespace("rl_inference"),
            "rate_limit_auth": redis.with_namespace("rl_auth"),
            "rate_limit_scim": redis.with_namespace("rl_scim"),
        }
    return stores


def _build_lifespan(
    database: Database,
    settings: Settings,
    trace_dispatcher: TraceDispatcher,
    metrics_aggregator: MetricsAggregator | None,
) -> list:
    return [
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
    ]


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
