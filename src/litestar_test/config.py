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
        )
