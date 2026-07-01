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

_PRODUCTION_ENVIRONMENTS = frozenset({"production", "prod"})


class InsecureConfigurationError(RuntimeError):
    """Raised at startup when a production deploy uses an insecure default."""


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
    # SSO via OIDC. No discovery URL ⇒ disabled. `oidc_admin_groups` (comma-sep)
    # maps IdP groups to platform admin.
    oidc_discovery_url: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_scopes: str = DEFAULT_OIDC_SCOPES
    oidc_admin_groups: tuple[str, ...] = ()

    @property
    def sso_enabled(self) -> bool:
        return bool(self.oidc_discovery_url and self.oidc_client_id)

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in _PRODUCTION_ENVIRONMENTS

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith(("postgresql", "postgres"))

    def __post_init__(self) -> None:
        # Fail fast rather than silently signing JWTs with a publicly known key.
        if self.is_production and (not self.jwt_secret or self.jwt_secret == DEFAULT_JWT_SECRET):
            raise InsecureConfigurationError(
                "JWT_SECRET must be set to a strong, non-default value in production"
            )

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()  # no-op if .env is absent
        return cls(
            database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
            admin_email=os.environ.get("ADMIN_EMAIL", DEFAULT_ADMIN_EMAIL),
            master_key=os.environ.get("MASTER_KEY"),
            jwt_secret=os.environ.get("JWT_SECRET", DEFAULT_JWT_SECRET),
            salt_key=os.environ.get("SALT_KEY"),
            environment=os.environ.get("ENVIRONMENT", DEFAULT_ENVIRONMENT),
            db_pool_size=int(os.environ.get("DB_POOL_SIZE", DEFAULT_DB_POOL_SIZE)),
            db_max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", DEFAULT_DB_MAX_OVERFLOW)),
            request_timeout=float(os.environ.get("REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT)),
            max_retries=int(os.environ.get("MAX_RETRIES", DEFAULT_MAX_RETRIES)),
            rotation_enabled=_env_bool("KEY_ROTATION_ENABLED", False),
            rotation_time=os.environ.get("KEY_ROTATION_TIME", DEFAULT_ROTATION_TIME),
            mlflow_tracking_uri=os.environ.get("MLFLOW_TRACKING_URI"),
            mlflow_experiment=os.environ.get("MLFLOW_EXPERIMENT", DEFAULT_MLFLOW_EXPERIMENT),
            oidc_discovery_url=os.environ.get("OIDC_DISCOVERY_URL"),
            oidc_client_id=os.environ.get("OIDC_CLIENT_ID"),
            oidc_client_secret=os.environ.get("OIDC_CLIENT_SECRET"),
            oidc_scopes=os.environ.get("OIDC_SCOPES", DEFAULT_OIDC_SCOPES),
            oidc_admin_groups=tuple(
                g.strip() for g in os.environ.get("OIDC_ADMIN_GROUPS", "").split(",") if g.strip()
            ),
        )
