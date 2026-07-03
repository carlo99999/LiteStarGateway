"""Application settings, loaded from environment variables (and an optional .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///api_keys.db"
DEFAULT_ADMIN_EMAIL = "admin@example.com"
DEFAULT_ENVIRONMENT = "development"
DEFAULT_DB_POOL_SIZE = 5
DEFAULT_DB_MAX_OVERFLOW = 10
# Upstream provider call resilience.
DEFAULT_REQUEST_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 2
# Daily key rotation (UTC time, "HH:MM"). Opt-in via KEY_ROTATION_ENABLED.
DEFAULT_ROTATION_TIME = "03:00"
# Observability. No tracking URI ⇒ tracing disabled (NullSink).
DEFAULT_MLFLOW_EXPERIMENT = "litestar-gateway"
# SSO (OIDC). No discovery URL ⇒ SSO disabled.
DEFAULT_OIDC_SCOPES = "openid email profile groups"


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
    # Connection-pool sizing (applied only to Postgres; SQLite ignores it).
    db_pool_size: int = DEFAULT_DB_POOL_SIZE
    db_max_overflow: int = DEFAULT_DB_MAX_OVERFLOW
    # Per-call timeout (seconds) and retry budget for upstream provider SDKs.
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    # Daily automatic key rotation (opt-in), at rotation_time (UTC, "HH:MM").
    rotation_enabled: bool = False
    rotation_time: str = DEFAULT_ROTATION_TIME
    # Observability: MLflow tracking URI (None ⇒ tracing disabled) + general experiment.
    mlflow_tracking_uri: str | None = None
    mlflow_experiment: str = DEFAULT_MLFLOW_EXPERIMENT
    # Serve the interactive OpenAPI docs (Swagger/Scalar/Stoplight + /openapi.json).
    # Public and unauthenticated when on — disable in production to avoid exposing
    # the full admin/credential API surface.
    openapi_enabled: bool = True
    # Mark the SSO state cookie `Secure` (HTTPS-only). Defaults on outside local
    # envs; set explicitly (SESSION_COOKIE_SECURE) when TLS terminates at a proxy
    # that speaks HTTP to the app, so the request scheme alone can't be trusted.
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
    # Public callback URL registered at the IdP. Set this when the app runs behind
    # a reverse proxy/ingress, where the request's own host/scheme is the internal
    # one. When None, the callback URL is derived from the incoming request.
    oidc_redirect_uri: str | None = None

    @property
    def sso_enabled(self) -> bool:
        # Confidential-client flow: the secret is mandatory. Missing it ⇒ SSO stays
        # off (routes unregistered) rather than booting a broken/public-client flow.
        return bool(self.oidc_discovery_url and self.oidc_client_id and self.oidc_client_secret)

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in _PRODUCTION_ENVIRONMENTS

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

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()  # no-op if .env is absent
        environment = os.environ.get("ENVIRONMENT", DEFAULT_ENVIRONMENT)
        is_local = environment.strip().lower() in _LOCAL_ENVIRONMENTS
        return cls(
            database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
            admin_email=os.environ.get("ADMIN_EMAIL", DEFAULT_ADMIN_EMAIL),
            master_key=os.environ.get("MASTER_KEY"),
            jwt_secret=os.environ.get("JWT_SECRET", DEFAULT_JWT_SECRET),
            salt_key=os.environ.get("SALT_KEY"),
            environment=environment,
            db_pool_size=_env_int("DB_POOL_SIZE", DEFAULT_DB_POOL_SIZE, minimum=1),
            db_max_overflow=_env_int("DB_MAX_OVERFLOW", DEFAULT_DB_MAX_OVERFLOW, minimum=0),
            request_timeout=_env_float("REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT, minimum=0.0),
            max_retries=_env_int("MAX_RETRIES", DEFAULT_MAX_RETRIES, minimum=0),
            rotation_enabled=_env_bool("KEY_ROTATION_ENABLED", False),
            rotation_time=os.environ.get("KEY_ROTATION_TIME", DEFAULT_ROTATION_TIME),
            mlflow_tracking_uri=os.environ.get("MLFLOW_TRACKING_URI"),
            mlflow_experiment=os.environ.get("MLFLOW_EXPERIMENT", DEFAULT_MLFLOW_EXPERIMENT),
            openapi_enabled=_env_bool("OPENAPI_ENABLED", True),
            # Secure cookies on by default outside local envs; overridable for
            # proxy topologies where the request scheme is HTTP behind TLS.
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
        )
