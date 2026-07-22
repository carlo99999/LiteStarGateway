"""Application settings, loaded from environment variables (and an optional .env)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from uuid import UUID

from dotenv import load_dotenv

from litestar_gateway.domain.entities import TeamRole

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


@dataclass(frozen=True)
class TeamGrant:
    """One (team, role) an IdP group confers via SSO_TEAM_MAPPING."""

    team_id: UUID
    role: TeamRole


def _env_team_mapping(name: str) -> dict[str, tuple[TeamGrant, ...]]:
    """Parse SSO_TEAM_MAPPING: a JSON object mapping each IdP group to a list of
    ``{"team": "<team-uuid>", "role": "admin"|"member"}`` grants (role defaults
    to member). Absent/empty ⇒ no mapping (SSO sets only the platform-admin
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
    mapping: dict[str, tuple[TeamGrant, ...]] = {}
    for group, grants in data.items():
        if not isinstance(grants, list):
            raise InsecureConfigurationError(f"{name}[{group!r}] must be a list of grants")
        parsed: list[TeamGrant] = []
        for grant in grants:
            if not isinstance(grant, dict) or "team" not in grant:
                raise InsecureConfigurationError(f"{name}[{group!r}] entries need a 'team' UUID")
            try:
                team_id = UUID(str(grant["team"]))
                role = TeamRole(grant.get("role", TeamRole.MEMBER))
            except ValueError as exc:
                raise InsecureConfigurationError(
                    f"{name}[{group!r}] has an invalid team or role: {exc}"
                ) from exc
            parsed.append(TeamGrant(team_id=team_id, role=role))
        mapping[group] = tuple(parsed)
    # Two different non-admin roles for one team would make the resolved role
    # depend on the IdP's group ordering (ADMIN always wins, so pairing it with
    # another role stays deterministic). Reject the ambiguity at startup.
    non_admin_roles: dict[UUID, TeamRole] = {}
    for grants_ in mapping.values():
        for grant_ in grants_:
            if grant_.role is TeamRole.ADMIN:
                continue
            seen = non_admin_roles.setdefault(grant_.team_id, grant_.role)
            if seen is not grant_.role:
                raise InsecureConfigurationError(
                    f"{name} maps team {grant_.team_id} to conflicting roles "
                    f"'{seen}' and '{grant_.role}'; grant one non-admin role per team"
                )
    return mapping


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

    def __post_init__(self) -> None:
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
            _env_bool("AUTO_CREATE_SCHEMA", False)
            if "AUTO_CREATE_SCHEMA" in os.environ
            else None
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
        )
