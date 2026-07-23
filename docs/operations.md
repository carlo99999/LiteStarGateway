# Operations guide

Running the gateway in production: proxying, database, migrations,
observability, and scaling. For the original container design rationale see
[deployment](deployment.md).

## Reverse proxy & TLS

The app expects to sit behind a reverse proxy that terminates TLS.

- Set `FORWARDED_ALLOW_IPS` to your proxy's IP/CIDR so the real client IP
  reaches the per-IP rate limit. The image default is loopback: forwarded
  headers from any other peer are ignored, so a direct client cannot forge its
  IP to bypass the auth rate limit or spoof the audit log.
- Preserve the public `Host` header when proxying the same-origin admin
  console: browser-session CSRF validation compares it with the browser's
  `Origin`.
- `SESSION_COOKIE_SECURE=true` is mandatory outside local environments so SSO
  and admin-session cookies remain HTTPS-only when TLS terminates at the proxy.
  It also enables the `Strict-Transport-Security` (HSTS) response header — the
  app emits it only when this signal is set, so a plain-HTTP run never pins the
  host to HTTPS.
- Configure the public OIDC callback URL from **Console → SSO** so it matches
  what is registered at the IdP. `OIDC_REDIRECT_URI` remains the legacy env
  fallback when no enabled DB-backed SSO configuration exists.
- The app sets static security response headers (`X-Content-Type-Options`,
  `X-Frame-Options`, `Referrer-Policy`) on every response. It does **not** emit
  a Content-Security-Policy — set one at the proxy if you want it, since a
  correct policy for the built SPA needs per-build nonces/hashes.

## Request limits

`MAX_BODY_SIZE` caps the accepted request body in bytes (default 10 MB, 413
above it). Lower it to tighten the DoS bound; raise it for large multimodal
payloads — inline base64 images push vision requests past a few MB.

## Database

Production requires **Postgres**. `docker-compose.yml` runs it
(`postgresql+asyncpg://…`) and the app connects to it. Pool sizing is
configurable via `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` (Postgres only). The image
ships no `DATABASE_URL` default: it must point at Postgres or the app refuses
to start (a SQLite default would give every replica its own database).

SQLite remains the zero-config dev/test default (single-writer, weak
concurrency); the app fails fast at startup if it sees SQLite with
`ENVIRONMENT=production`.

## Migrations

Production uses Alembic (`create_all` is dev/test only). The container applies
pending migrations on start (`litestar … database upgrade`, idempotent). After
changing the ORM models, generate a migration in dev with
`uv run litestar --app litestar_gateway.app:app database make-migrations`,
review it, and commit it.

**With many replicas**, set `MIGRATE_ON_START=false` on the app containers and
run the upgrade as a dedicated one-shot job / init container instead, so N
replicas don't race the same upgrade:

```bash
docker run --rm <image> litestar --app litestar_gateway.app:app database upgrade --no-prompt
```

See [db-migrations](db-migrations.md) for the design.

## Observability

Set `MLFLOW_TRACKING_URI` to enable request tracing (classic MLflow or
`databricks`). The compose stack runs a classic MLflow server (UI at
`http://localhost:5000`) and points the app at it; unset the URI to disable.
See [observability](observability.md).

## Multi-process / replicas

Set `REDIS_URL` to back the rate-limit store with a shared Redis so limits hold
across workers/replicas (the compose stack includes a `redis` service and sets
it; drop the var to fall back to the in-memory per-process store). `REDIS_URL`
also enables a distributed lock so only one replica runs the daily key rotation
(without it, rotation assumes a single instance). The trace queue is still
per-process (each drains its own).

## Local development stack

For full-stack local development with live reload, run:

```bash
just dev
```

The first run creates `.env.docker-dev` with random, gitignored local secrets
and starts Postgres, Redis, MLflow, the Litestar backend, and the Vite
frontend. Open the admin console at `http://127.0.0.1:5173/ui/`; API requests
are proxied to the backend at `http://127.0.0.1:8000`. Python changes trigger
Litestar reload, while React/TypeScript/CSS changes use Vite HMR. Stop the
stack with `just dev-down`; volumes are preserved. Changes to dependency
manifests or lockfiles require rebuilding the images.

If filesystem events are not propagated by the Docker host, set
`WATCHFILES_FORCE_POLLING=true` and/or `CHOKIDAR_USEPOLLING=true` before
running `just dev`. Host ports can be changed when a default is occupied, for
example `BACKEND_PORT=18000 FRONTEND_PORT=15173 just dev`; the container
network and frontend API proxy continue to use their fixed internal ports.

For a prod-like compose run on a different host port, set `APP_PORT` (for
example `APP_PORT=18000 docker compose up --build`).
