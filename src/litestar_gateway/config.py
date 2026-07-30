"""Application settings, loaded from environment variables (and an optional .env)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from litestar_gateway.domain.egress_policy import EgressAllowlist, parse_allowlist
from litestar_gateway.domain.entities import TeamGrant, parse_team_mapping

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///gateway.db"
DEFAULT_ADMIN_EMAIL = "admin@example.com"
DEFAULT_ENVIRONMENT = "development"
DEFAULT_DB_POOL_SIZE = 5
DEFAULT_DB_MAX_OVERFLOW = 10
# Upstream provider call resilience.
DEFAULT_REQUEST_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 2
# Max accepted request body (bytes). Matches Litestar's own default, made
# explicit + tunable: lower it to tighten the DoS bound, raise it for large
# multimodal payloads (inline base64 images push vision requests past a few MB).
DEFAULT_MAX_BODY_SIZE = 10_000_000
# Pre-auth inference flood guard. Production may raise this explicitly when a
# trusted ingress provides its own limit; the conservative default stays intact.
DEFAULT_INFERENCE_RATE_LIMIT_RPM = 120
# Cross-provider failover circuit breaker (Plan 05 Phase 3, optional): consecutive
# failures before a candidate is short-circuited, and the cooldown before it gets
# a half-open retry trial.
DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
DEFAULT_CIRCUIT_BREAKER_COOLDOWN_SECONDS = 30
# Response cache (Plan 04 Phase 0): off by default (global kill-switch), plus a
# per-team/per-model opt-in (Model.cache_enabled). TTL bounds staleness;
# max-entries bounds the in-memory fallback's size.
DEFAULT_RESPONSE_CACHE_TTL_S = 3600
DEFAULT_RESPONSE_CACHE_MAX_ENTRIES = 10_000
# Semantic tier (Plan 04 Phase 2): a near-duplicate prompt at/above this cosine
# similarity, within the caller's own tenant scope, is served like an exact hit.
# Per-team/model opt-in (Model.cache_semantic_enabled), tried only on an
# exact-match miss — never a replacement for it.
DEFAULT_RESPONSE_CACHE_SEMANTIC_THRESHOLD = 0.97
# Daily key rotation (UTC time, "HH:MM"). Opt-in via KEY_ROTATION_ENABLED.
DEFAULT_ROTATION_TIME = "03:00"
# Observability. No tracking URI ⇒ tracing disabled (NullSink).
DEFAULT_MLFLOW_EXPERIMENT = "litestar-gateway"
# SSO (OIDC). No discovery URL ⇒ SSO disabled.
DEFAULT_OIDC_SCOPES = "openid email profile groups"
# Platform role a brand-new SSO user is provisioned with at first login (JIT),
# when not matched by OIDC_ADMIN_GROUPS. The platform role is binary.
DEFAULT_PLATFORM_ROLE = "member"
_PLATFORM_ROLES = frozenset({"admin", "member"})
# Budget-alert webhook delivery (Plan 07 Phase 2): matches the routing
# webhook's own default call timeout (`application/routing/webhook.py`'s
# DEFAULT_TIMEOUT_MS).
DEFAULT_BUDGET_ALERT_WEBHOOK_TIMEOUT_MS = 2000
# Budget-alert email delivery (Plan 07 Phase 3, design doc §4/§8). Platform-wide
# SMTP server config; the per-team recipient is data on the team's budget, not
# config here. 587 is the STARTTLS submission port (default when SMTP_USE_TLS).
DEFAULT_SMTP_PORT = 587
# Retention/anonymization window (Plan 13 Phase 5, docs/next-steps/billing-integrity.md
# §5): days after a team is soft-deleted (tombstoned) before its ledger PII/
# attribution is eligible for anonymization. Documents the policy and bounds a
# future anonymization job; this phase does not run that job automatically —
# see the design doc for what "eligible" means and why.
DEFAULT_TEAM_RETENTION_DAYS = 90


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


# ≥32 bytes to satisfy HS256 key-length recommendations. Override in production.
DEFAULT_JWT_SECRET = "dev-insecure-change-me-please-0123456789"

# The MASTER_KEY placeholder shipped in .env.sample. It becomes the platform
# admin's password on first boot, so a forgotten override must never make it
# past startup outside local envs.
SAMPLE_MASTER_KEY = "change-me-please"

_PRODUCTION_ENVIRONMENTS = frozenset({"production", "prod"})
# Explicitly-local environments where insecure defaults are tolerated. Anything
# NOT in this set (production, staging, a typo, …) is treated as security-sensitive.
_LOCAL_ENVIRONMENTS = frozenset({"development", "dev", "test", "local"})
# Minimum length for configured secrets outside local envs. The envelope-encryption
# master key is derived from these via SHA-256, so their entropy must come from
# length/randomness — a short passphrase would be brute-forceable.
MIN_SECRET_LENGTH = 32


class InsecureConfigurationError(RuntimeError):
    """Raised at startup when a non-local deploy uses an insecure default."""


def _env_team_mapping(name: str) -> dict[str, tuple[TeamGrant, ...]]:
    """Parse SSO_TEAM_MAPPING: see `domain.entities.parse_team_mapping` for the
    grant shape. Absent/empty ⇒ no mapping (SSO sets only the platform-admin
    flag). Malformed input fails fast at startup rather than silently dropping
    grants."""
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InsecureConfigurationError(f"{name} must be valid JSON") from exc
    if not isinstance(data, dict):
        raise InsecureConfigurationError(f"{name} must be a JSON object of group -> grants")
    try:
        return parse_team_mapping(data)
    except ValueError as exc:
        raise InsecureConfigurationError(f"{name}: {exc}") from exc


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise InsecureConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise InsecureConfigurationError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise InsecureConfigurationError(f"{name} must be a number, got {raw!r}") from exc
    if value <= minimum:
        raise InsecureConfigurationError(f"{name} must be > {minimum}, got {value}")
    return value


def _env_choice(name: str, default: str, choices: frozenset[str]) -> str:
    # Case-insensitive; a typo fails fast at startup (in every environment, not
    # just non-local) rather than silently falling back to the default.
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value not in choices:
        raise InsecureConfigurationError(f"{name} must be one of {sorted(choices)}, got {raw!r}")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str
    admin_email: str
    # Bootstrap password for the admin user. Required only when the users table
    # is empty; absence + empty table raises at startup.
    master_key: str | None
    # Secret used to sign login JWTs. MUST be overridden in production — leaving it
    # at the dev default in production fails fast (see __post_init__).
    jwt_secret: str
    # Encryption key for credential values at rest. No default (a fixed key would
    # defeat encryption); credential operations fail clearly if it is unset.
    salt_key: str | None
    # Deployment environment. "production"/"prod" enables fail-fast config checks.
    environment: str = DEFAULT_ENVIRONMENT
    # Create the schema from ORM metadata on startup. None ⇒ derive from the
    # environment (`not is_production`); set explicitly to override. The dev
    # container sets it False (it owns the schema via `database upgrade`, and
    # running both races to create the same new tables). Read via
    # `should_create_schema`, never this raw field.
    auto_create_schema: bool | None = None
    # Connection-pool sizing (applied only to Postgres; SQLite ignores it).
    db_pool_size: int = DEFAULT_DB_POOL_SIZE
    db_max_overflow: int = DEFAULT_DB_MAX_OVERFLOW
    # Per-call timeout (seconds) and retry budget for upstream provider SDKs.
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    # Reject request bodies larger than this many bytes (413) before they're read.
    max_body_size: int = DEFAULT_MAX_BODY_SIZE
    inference_rate_limit_rpm: int = DEFAULT_INFERENCE_RATE_LIMIT_RPM
    # Cross-provider failover circuit breaker (optional; see build_circuit_breaker).
    circuit_breaker_failure_threshold: int = DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD
    circuit_breaker_cooldown_seconds: int = DEFAULT_CIRCUIT_BREAKER_COOLDOWN_SECONDS
    # Response cache (Plan 04 Phase 0). Global kill-switch, off by default; a
    # request only ever participates when this AND the model's own
    # `cache_enabled` are both true.
    response_cache_enabled: bool = False
    response_cache_ttl_s: int = DEFAULT_RESPONSE_CACHE_TTL_S
    response_cache_max_entries: int = DEFAULT_RESPONSE_CACHE_MAX_ENTRIES
    # Semantic tier (Plan 04 Phase 2). `response_cache_semantic_embedding_model`
    # is the *name* of the embeddings model each team must configure to use the
    # semantic tier (resolved per-team, same as the S3 routing embeddings
    # strategy's `embedding_model` config field); None ⇒ the semantic tier is
    # inert everywhere even for models that opted in (design §8 — a missing
    # embedder is treated as semantic-ineligible, never an error).
    response_cache_semantic_threshold: float = DEFAULT_RESPONSE_CACHE_SEMANTIC_THRESHOLD
    response_cache_semantic_embedding_model: str | None = None
    # Daily automatic key rotation (opt-in), at rotation_time (UTC, "HH:MM").
    rotation_enabled: bool = False
    rotation_time: str = DEFAULT_ROTATION_TIME
    # Observability: MLflow tracking URI (None ⇒ tracing disabled) + general experiment.
    mlflow_tracking_uri: str | None = None
    mlflow_experiment: str = DEFAULT_MLFLOW_EXPERIMENT
    # Fleet-level ops metrics logged to an MLflow "gateway-metrics" run every N
    # seconds (requires the tracking URI; 0 disables the publisher).
    mlflow_metrics_interval: int = 60
    # Serve the interactive OpenAPI docs (Swagger/Scalar/Stoplight + /openapi.json).
    # Public and unauthenticated when on — disable in production to avoid exposing
    # the full admin/credential API surface.
    openapi_enabled: bool = True
    # Mark browser-session and SSO cookies `Secure` (HTTPS-only). Mandatory
    # outside local envs because a TLS-terminating proxy may speak HTTP to the app,
    # so the request scheme alone cannot be trusted.
    session_cookie_secure: bool = False
    # Optional Redis backing for the rate-limit store, shared across replicas. When
    # unset, an in-memory per-process store is used (fine for a single instance).
    redis_url: str | None = None
    # How long an admitted-but-unsettled request holds its slice of a team's
    # budget before the store reclaims it. Covers a slow streamed completion;
    # bounds how long a replica killed mid-request can strand headroom.
    budget_reservation_ttl_s: int = 300
    # Hostnames this deployment answers to. Several places derive a URL from the
    # request's `Host` (the SSO callback when no fixed one is set is the one that
    # produced ISSUE-028/032), so accepting any host is only defensible on
    # localhost. `*.example.com` matches subdomains.
    allowed_hosts: tuple[str, ...] = ()
    # Trusted reverse-proxy allowlist (IPs/CIDRs, comma-separated) for the
    # request-correlation middleware (Plan 11 Slice A, docs/logging.md §2): an
    # inbound `X-Request-ID` is trusted verbatim only when the direct connecting
    # peer is in this list. Mirrors `FORWARDED_ALLOW_IPS` (the ASGI-server-level
    # trusted-proxy concept documented in docs/operations.md) but must live in
    # app config too, since the app itself — not just uvicorn — needs to decide
    # whether to trust the header. Empty ⇒ no inbound id is ever trusted; every
    # request gets a freshly generated one.
    trusted_proxy_ips: tuple[str, ...] = ()
    # Egress allowlist for the `openai_compatible` provider (Plan 18): the only
    # targets a credential of that provider may point `api_base` at. Entries are
    # `<host|ip|cidr>[:port]`, comma-separated; see domain/egress_policy.py for
    # the grammar. Unlike every other outbound target the gateway has, these are
    # *expected* to be private (a self-hosted model server), so the SSRF
    # deny-list cannot be applied — this list is what constrains them instead.
    # Empty ⇒ the provider is unusable, so a deployment that upgrades gains no
    # new egress reach until an operator opts in.
    openai_compatible_allowed_hosts: tuple[str, ...] = ()
    # Egress allowlist for MCP tool servers (Plan 20). Same grammar and parser as
    # the provider allowlist above, and deliberately a *separate* list: a host
    # authorized to serve a self-hosted model is not thereby authorized to
    # receive tool arguments, which is different data leaving the gateway. This
    # is the platform's one veto over a team-owned resource — a team admin
    # registers servers freely, but only inside this list.
    mcp_allowed_hosts: tuple[str, ...] = ()
    # How long a discovered tool inventory stays fresh (Plan 20). A refresh
    # inside this window returns the stored inventory instead of calling the
    # server again, so a console that refreshes on page load does not turn each
    # visit into outbound traffic. `force` overrides it for the operator who
    # knows the server changed.
    mcp_inventory_ttl_seconds: int = 3600
    # SSO via OIDC. No discovery URL ⇒ disabled. `oidc_admin_groups` (comma-sep)
    # maps IdP groups to platform admin.
    oidc_discovery_url: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_scopes: str = DEFAULT_OIDC_SCOPES
    oidc_admin_groups: tuple[str, ...] = ()
    # SSO_TEAM_MAPPING: IdP group -> teams+roles (see _env_team_mapping). Teams
    # named here are "SSO-governed": the user's membership tracks their IdP groups
    # on every login. Teams absent from the mapping are left to manual management.
    oidc_team_mapping: dict[str, tuple[TeamGrant, ...]] = field(default_factory=dict)
    # Public callback URL registered at the IdP. Set this when the app runs behind
    # a reverse proxy/ingress, where the request's own host/scheme is the internal
    # one. When None, the callback URL is derived from the incoming request.
    oidc_redirect_uri: str | None = None
    # Platform role a brand-new SSO user receives at first login (JIT) when not
    # matched by OIDC_ADMIN_GROUPS: "member" (default) or "admin". The admin flag
    # is upgrade-only — re-login never downgrades it (see UserService), so demotion
    # is the explicit job of the platform-admin endpoint.
    default_role: str = DEFAULT_PLATFORM_ROLE
    # Budget-alert webhook delivery (Plan 07 Phase 2, design doc §4/§8). A
    # single platform-wide target for now: every team's fired threshold
    # alert is POSTed here. None ⇒ the delivery worker isn't started at all
    # (nothing to drain into yet — Phase 1's outbox just queues quietly).
    # Per-team targets are Phase 3's config-surface work on the existing
    # budget endpoints (`GET/PUT/DELETE /teams/{id}/budget`), not this.
    budget_alert_webhook_url: str | None = None
    budget_alert_webhook_bearer_token: str | None = None
    # HMAC key for signing OUTBOUND webhook payloads (budget alerts and, unless
    # its strategy_config overrides it, the routing webhook). Without it we
    # cannot sign, and a receiver cannot tell our traffic from anyone else's who
    # learned the URL. Platform-wide for now: per-endpoint secrets for the
    # per-team alert URL need a column, hence a migration, and are follow-up.
    webhook_signing_secret: str | None = None
    budget_alert_webhook_timeout_ms: int = DEFAULT_BUDGET_ALERT_WEBHOOK_TIMEOUT_MS
    # Budget-alert email delivery (Plan 07 Phase 3, design doc §4/§8). A single
    # platform-wide SMTP server; a team opts in per-budget by setting
    # `Budget.alert_email` (the recipient). Without a host + from-address,
    # email delivery is disabled and any team's `alert_email` simply never
    # dispatches — the per-team webhook still works independently.
    smtp_host: str | None = None
    smtp_port: int = DEFAULT_SMTP_PORT
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    smtp_from_address: str | None = None
    # Retention/anonymization window, in days, for a soft-deleted team's ledger
    # attribution (Plan 13 Phase 5). Structural/documentation only this phase —
    # no background job reads it yet; it's here so that job has a config surface
    # to land on without another migration.
    team_retention_days: int = DEFAULT_TEAM_RETENTION_DAYS

    @property
    def smtp_configured(self) -> bool:
        """Whether the platform can send budget-alert email at all. Both a
        server host and a From address are required; missing either disables
        the email channel (per-team `alert_email` values are then ignored)."""
        return bool(self.smtp_host and self.smtp_from_address)

    @property
    def budget_alert_delivery_configured(self) -> bool:
        """Whether any budget-alert delivery capability exists platform-wide,
        gating whether the outbox worker is started at all (mirroring Phase 2's
        `budget_alert_webhook_url`-only gate). Per-team webhook overrides ride
        on the same worker, so a platform webhook OR SMTP being configured is
        enough to start it."""
        return bool(self.budget_alert_webhook_url) or self.smtp_configured

    @property
    def default_admin(self) -> bool:
        """Whether a first-login SSO user defaults to platform admin (DEFAULT_ROLE)."""
        return self.default_role == "admin"

    @property
    def sso_enabled(self) -> bool:
        # Confidential-client flow: the secret is mandatory. Missing it ⇒ SSO stays
        # off (routes unregistered) rather than booting a broken/public-client flow.
        return bool(self.oidc_discovery_url and self.oidc_client_id and self.oidc_client_secret)

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in _PRODUCTION_ENVIRONMENTS

    @property
    def should_create_schema(self) -> bool:
        """Whether to auto-create the schema on startup: the explicit override
        if set, else on everywhere except production (which uses migrations)."""
        if self.auto_create_schema is not None:
            return self.auto_create_schema
        return not self.is_production

    @property
    def is_local(self) -> bool:
        return self.environment.strip().lower() in _LOCAL_ENVIRONMENTS

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith(("postgresql", "postgres"))

    def egress_allowlist(self) -> EgressAllowlist:
        """The parsed `openai_compatible` egress allowlist (Plan 18). Empty
        unless an operator opted in, which makes that provider unusable."""
        return parse_allowlist(self.openai_compatible_allowed_hosts)

    def mcp_allowlist(self) -> EgressAllowlist:
        """The parsed MCP tool-server allowlist (Plan 20). Empty unless an
        operator opted in, so no team can register a server yet."""
        return parse_allowlist(self.mcp_allowed_hosts)

    def __post_init__(self) -> None:
        # Before the local-env shortcut below: a malformed allowlist entry is a
        # typo, not an insecurity, and the environment most likely to catch it
        # is the developer's. Silently dropping it would leave an operator
        # believing a target is authorized when it is not.
        self.egress_allowlist()
        self.mcp_allowlist()
        # Fail fast on insecure secrets everywhere except explicitly-local envs, so a
        # staging or misspelled environment cannot silently run on the public default
        # or a brute-forceable short key.
        if self.is_local:
            return
        if not self.session_cookie_secure:
            raise InsecureConfigurationError(
                "SESSION_COOKIE_SECURE must be true outside local environments"
            )
        # Production runs on PostgreSQL, full stop. SQLite is single-writer,
        # per-container storage: with N replicas each one gets its own silently
        # diverging database, and an unmounted volume loses everything on
        # restart. The image ships no DATABASE_URL default, so forgetting to
        # set it fails here instead of booting broken storage.
        if self.is_production and not self.is_postgres:
            raise InsecureConfigurationError(
                "Production requires PostgreSQL: set DATABASE_URL to a "
                "postgresql+asyncpg:// URL (SQLite is for local development only)"
            )
        # Production runs on Redis for the same reason. Without it, every shared
        # component falls back to per-process state: rate limits become N x the
        # configured value with N replicas, the circuit breaker keeps a private
        # state machine per replica (the divergence behind ISSUE-029), and the
        # response cache stops being shared. An infrastructural dependency, not
        # a preference — so it fails here rather than degrading silently.
        # Deployed non-production environments keep the warning in `app.py`:
        # single-replica staging is legitimate and should stay easy to stand up.
        # A deployed gateway knows its own hostnames. Without the list, any
        # `Host` is accepted and anything derived from it is attacker-influenced
        # — the class behind ISSUE-028 and ISSUE-032, rather than either
        # instance.
        if not self.allowed_hosts:
            raise InsecureConfigurationError(
                "ALLOWED_HOSTS must list the hostnames this deployment answers to "
                "outside local environments (otherwise any Host header is accepted, "
                "and URLs derived from it are attacker-controlled)"
            )
        # A single `*` anywhere disables the check for the whole list — the
        # middleware short-circuits on it and compiles no pattern at all — so a
        # config that looks specific would not be. It is also the obvious thing
        # to write to get past this very setting, which would leave the
        # deployment exactly where the setting exists to move it from.
        # `*.example.com` is a real constraint and stays allowed.
        if any(host.strip() == "*" for host in self.allowed_hosts):
            raise InsecureConfigurationError(
                "ALLOWED_HOSTS must not contain '*' outside local environments: it "
                "accepts every Host header, which is what this setting exists to "
                "prevent. List the hostnames this deployment answers to, or use "
                "'*.example.com' to match subdomains"
            )
        if self.is_production and not self.redis_url:
            raise InsecureConfigurationError(
                "Production requires Redis: set REDIS_URL (without it rate limits, "
                "the circuit breaker and the response cache are per-process, so "
                "every replica enforces its own private limits)"
            )
        if not self.jwt_secret or self.jwt_secret == DEFAULT_JWT_SECRET:
            raise InsecureConfigurationError(
                "JWT_SECRET must be set to a strong, non-default value outside local environments"
            )
        if len(self.jwt_secret) < MIN_SECRET_LENGTH:
            raise InsecureConfigurationError(
                f"JWT_SECRET must be at least {MIN_SECRET_LENGTH} characters"
            )
        # SALT_KEY is optional (credential encryption is opt-in), but if set it wraps
        # the credential keyring, so it must be strong too.
        if self.salt_key is not None and len(self.salt_key) < MIN_SECRET_LENGTH:
            raise InsecureConfigurationError(
                f"SALT_KEY must be at least {MIN_SECRET_LENGTH} characters when set"
            )
        # MASTER_KEY is optional (only needed to bootstrap an empty users table),
        # but when set it becomes the platform admin's password — the sample
        # placeholder or a short passphrase would hand over the whole gateway.
        if self.master_key is not None:
            if self.master_key == SAMPLE_MASTER_KEY:
                raise InsecureConfigurationError(
                    "MASTER_KEY is the .env.sample placeholder; set a strong random value"
                )
            if len(self.master_key) < MIN_SECRET_LENGTH:
                raise InsecureConfigurationError(
                    f"MASTER_KEY must be at least {MIN_SECRET_LENGTH} characters when set"
                )
        # With SSO enabled outside local dev, require an explicit callback URL.
        # Otherwise sso.py derives redirect_uri from the request's Host header,
        # so a forged Host steers the OIDC redirect declared in the authorization
        # request — exploitable against IdPs with non-exact redirect matching (M31).
        if self.sso_enabled and not self.oidc_redirect_uri:
            raise InsecureConfigurationError(
                "OIDC_REDIRECT_URI must be set when SSO is enabled outside local "
                "environments (otherwise the callback URL is derived from the "
                "untrusted Host header)"
            )

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()  # no-op if .env is absent
        environment = os.environ.get("ENVIRONMENT", DEFAULT_ENVIRONMENT)
        is_local = environment.strip().lower() in _LOCAL_ENVIRONMENTS
        # None unless explicitly set, so `should_create_schema` derives from env.
        auto_create_schema = (
            _env_bool("AUTO_CREATE_SCHEMA", False) if "AUTO_CREATE_SCHEMA" in os.environ else None
        )
        return cls(
            database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
            admin_email=os.environ.get("ADMIN_EMAIL", DEFAULT_ADMIN_EMAIL),
            master_key=os.environ.get("MASTER_KEY"),
            jwt_secret=os.environ.get("JWT_SECRET", DEFAULT_JWT_SECRET),
            salt_key=os.environ.get("SALT_KEY"),
            environment=environment,
            auto_create_schema=auto_create_schema,
            db_pool_size=_env_int("DB_POOL_SIZE", DEFAULT_DB_POOL_SIZE, minimum=1),
            db_max_overflow=_env_int("DB_MAX_OVERFLOW", DEFAULT_DB_MAX_OVERFLOW, minimum=0),
            request_timeout=_env_float("REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT, minimum=0.0),
            max_retries=_env_int("MAX_RETRIES", DEFAULT_MAX_RETRIES, minimum=0),
            max_body_size=_env_int("MAX_BODY_SIZE", DEFAULT_MAX_BODY_SIZE, minimum=1),
            inference_rate_limit_rpm=_env_int(
                "INFERENCE_RATE_LIMIT_RPM",
                DEFAULT_INFERENCE_RATE_LIMIT_RPM,
                minimum=1,
            ),
            circuit_breaker_failure_threshold=_env_int(
                "CIRCUIT_BREAKER_FAILURE_THRESHOLD",
                DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
                minimum=1,
            ),
            circuit_breaker_cooldown_seconds=_env_int(
                "CIRCUIT_BREAKER_COOLDOWN_SECONDS",
                DEFAULT_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
                minimum=1,
            ),
            response_cache_enabled=_env_bool("RESPONSE_CACHE_ENABLED", False),
            response_cache_ttl_s=_env_int(
                "RESPONSE_CACHE_TTL_S", DEFAULT_RESPONSE_CACHE_TTL_S, minimum=1
            ),
            response_cache_max_entries=_env_int(
                "RESPONSE_CACHE_MAX_ENTRIES", DEFAULT_RESPONSE_CACHE_MAX_ENTRIES, minimum=1
            ),
            response_cache_semantic_threshold=_env_float(
                "RESPONSE_CACHE_SEMANTIC_THRESHOLD",
                DEFAULT_RESPONSE_CACHE_SEMANTIC_THRESHOLD,
                minimum=0.0,
            ),
            response_cache_semantic_embedding_model=os.environ.get(
                "RESPONSE_CACHE_SEMANTIC_EMBEDDING_MODEL"
            ),
            rotation_enabled=_env_bool("KEY_ROTATION_ENABLED", False),
            rotation_time=os.environ.get("KEY_ROTATION_TIME", DEFAULT_ROTATION_TIME),
            mlflow_tracking_uri=os.environ.get("MLFLOW_TRACKING_URI"),
            mlflow_experiment=os.environ.get("MLFLOW_EXPERIMENT", DEFAULT_MLFLOW_EXPERIMENT),
            mlflow_metrics_interval=_env_int("MLFLOW_METRICS_INTERVAL", 60, minimum=0),
            openapi_enabled=_env_bool("OPENAPI_ENABLED", True),
            # Secure cookies are mandatory outside local environments. Local HTTPS
            # requests also force Secure at response time.
            session_cookie_secure=_env_bool("SESSION_COOKIE_SECURE", not is_local),
            redis_url=os.environ.get("REDIS_URL"),
            budget_reservation_ttl_s=_env_int("BUDGET_RESERVATION_TTL_SECONDS", 300, minimum=1),
            allowed_hosts=tuple(
                h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()
            ),
            trusted_proxy_ips=tuple(
                v.strip() for v in os.environ.get("TRUSTED_PROXY_IPS", "").split(",") if v.strip()
            ),
            openai_compatible_allowed_hosts=tuple(
                v.strip()
                for v in os.environ.get("OPENAI_COMPATIBLE_ALLOWED_HOSTS", "").split(",")
                if v.strip()
            ),
            oidc_discovery_url=os.environ.get("OIDC_DISCOVERY_URL"),
            oidc_client_id=os.environ.get("OIDC_CLIENT_ID"),
            oidc_client_secret=os.environ.get("OIDC_CLIENT_SECRET"),
            oidc_scopes=os.environ.get("OIDC_SCOPES", DEFAULT_OIDC_SCOPES),
            oidc_admin_groups=tuple(
                g.strip() for g in os.environ.get("OIDC_ADMIN_GROUPS", "").split(",") if g.strip()
            ),
            oidc_redirect_uri=os.environ.get("OIDC_REDIRECT_URI"),
            default_role=_env_choice("DEFAULT_ROLE", DEFAULT_PLATFORM_ROLE, _PLATFORM_ROLES),
            oidc_team_mapping=_env_team_mapping("SSO_TEAM_MAPPING"),
            budget_alert_webhook_url=os.environ.get("BUDGET_ALERT_WEBHOOK_URL"),
            budget_alert_webhook_bearer_token=os.environ.get("BUDGET_ALERT_WEBHOOK_BEARER_TOKEN"),
            webhook_signing_secret=os.environ.get("WEBHOOK_SIGNING_SECRET"),
            budget_alert_webhook_timeout_ms=_env_int(
                "BUDGET_ALERT_WEBHOOK_TIMEOUT_MS",
                DEFAULT_BUDGET_ALERT_WEBHOOK_TIMEOUT_MS,
                minimum=1,
            ),
            smtp_host=os.environ.get("SMTP_HOST"),
            smtp_port=_env_int("SMTP_PORT", DEFAULT_SMTP_PORT, minimum=1),
            smtp_username=os.environ.get("SMTP_USERNAME"),
            smtp_password=os.environ.get("SMTP_PASSWORD"),
            smtp_use_tls=_env_bool("SMTP_USE_TLS", True),
            smtp_from_address=os.environ.get("SMTP_FROM_ADDRESS"),
            team_retention_days=_env_int(
                "TEAM_RETENTION_DAYS", DEFAULT_TEAM_RETENTION_DAYS, minimum=1
            ),
        )
