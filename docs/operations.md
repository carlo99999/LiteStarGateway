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
`databricks`). The default development and production Compose stacks do not
bundle MLflow; point the variable at an externally operated service when
tracing is required, or leave it empty to disable observability.
See [observability](observability.md).

## Capacity checks

Use the deterministic contract for gateway changes. It builds the production
image with the UI, starts an isolated tmpfs Postgres, Redis, and a private
OpenAI-compatible mock, bootstraps the model through the public management API,
then destroys the stack and its data:

```bash
just load-contract
```

No provider credential, existing model, billable call, or development database
is involved. Private bootstrap secrets live in the ignored `.env.benchmark`
file with mode `0600`; the generated team API key stays in process memory and
is neither printed nor written to reports.

The default contract runs independent complete-response and SSE stages at 25,
50, 100, 150, 200, 250, and 300 completed RPS. Each stage has 10 seconds of
ramp, 5 seconds of settle, and 60 seconds of measured steady state. It sizes
concurrency for one-second chat and three-second stream latency plus 25%
headroom, so the load generator does not become the bottleneck when the gateway
queues. Select one mode or collect every requested stage after failures with:

```bash
LOAD_MODES=chat just load-contract
LOAD_MODES=chat-stream LOAD_PROFILE_POLICY=diagnostic just load-contract
```

`fail-fast` is the acceptance default; `diagnostic` continues but still exits
non-zero if any stage misses its gate. Mode-specific limits are
`LOAD_CHAT_MAX_P95_MS`, `LOAD_STREAM_MAX_P95_MS`, and
`LOAD_STREAM_MAX_TTFT_MS` (defaults: 500 ms, 750 ms, and 750 ms). A failure ratio exactly at
`LOAD_MAX_FAILURE_RATIO` passes; only a value above it fails. This explicit gate
overrides Locust's default any-failure exit behavior.

The mock accepts `LOAD_MOCK_TTFT_MS`, `LOAD_MOCK_CHUNK_INTERVAL_MS`,
`LOAD_MOCK_TOTAL_LATENCY_MS`, and `LOAD_MOCK_CHUNK_COUNT`. It can inject every
Nth upstream failure deterministically. For example, this run should fail a 4%
threshold because every twentieth upstream request fails:

```bash
LOAD_MODES=chat LOAD_STAGES=25 \
LOAD_DURATION_SECONDS=60 LOAD_MOCK_FAILURE_EVERY=20 \
LOAD_MAX_FAILURE_RATIO=0.04 just load-contract
```

Every run writes ignored CSV/HTML results plus `metadata.json` and
`resources.jsonl` under `load-results/<timestamp>/`. Metadata is allowlisted and
includes the commit, dirty bit, selected contract, worker count, container image
IDs and CPU/memory limits. Resource samples contain gateway and mock CPU/RSS.
Internal event-loop, DB-pool, Redis and provider timing belongs to the profiling
instrumentation phase because adding it changes the production hot path.

Use the live-provider profile only to measure the real upstream/network path.
Run it against the production image, never the reload-enabled development
backend. The live script stops the development stack, generates the ignored
Compose overlay, and starts the production image against the existing
development Postgres volume:

```bash
just load-prod-up
just load-smoke

read -rs LOAD_API_KEY
export LOAD_API_KEY
export LOAD_MODEL=my-configured-model-or-router
export LOAD_CONFIRM_PROVIDER_COST=YES
just load-300
```

`load-smoke` checks `/health/ready` at 10 RPS. `load-300` runs independent
25, 50, 100, 150, 200, 250, and 300 RPS stages, first for complete JSON
responses and then for SSE streaming responses. Each stage ramps for 10 seconds,
settles for 5 seconds, measures a 60-second steady window, and writes its own
ignored CSV/HTML report under a timestamped `load-results/` directory. The
profile stops at the first failed stage unless
`LOAD_PROFILE_POLICY=diagnostic` is selected. Use `LOAD_MODES=chat` or
`LOAD_MODES=chat-stream` to run one path.

The explicit confirmation is required because the default profile can make more
than one hundred thousand billable provider calls. Check upstream quotas and
cost first.
The API key is read only from the environment and is never included in command
arguments, request names, output, or reports. Locust and all of its transitive
dependencies are installed from `uv.lock` before the key is passed to the test
process.

The runner also refuses a profile whose conservative bound exceeds 600,000
provider attempts or 100,000,000 total input-plus-output tokens. The estimate
assumes up to three provider attempts per gateway request and includes the
prompt. Override `LOAD_PROVIDER_MAX_ATTEMPTS` only after verifying the target's
retry configuration; the ignored local overlay sets `MAX_RETRIES=0`, so its
tighter value is `1`. The hard ceilings can be lowered with
`LOAD_MAX_PROVIDER_REQUESTS` and `LOAD_MAX_PROVIDER_TOKENS`.

Set the mode-specific expected latency variables to realistic provider values.
The runner allocates approximately:

```text
users = target RPS × expected end-to-end latency × 1.25 headroom
```

The recipes default to one second for complete responses and three seconds for
streams. Override `LOAD_CHAT_EXPECTED_LATENCY_SECONDS` and
`LOAD_STREAM_EXPECTED_LATENCY_SECONDS` after measuring the provider. For
example, 300 RPS at one second uses 375 users; at five seconds it uses 1,875.
The process exits non-zero when successful completed RPS falls below 98% of the
stage target, failures exceed 0.1%, end-to-end p95 exceeds the selected
`LOAD_CHAT_MAX_P95_MS` or `LOAD_STREAM_MAX_P95_MS`, or streaming TTFT p95
exceeds `LOAD_STREAM_MAX_TTFT_MS`. A streaming response counts exactly once and
only after a valid `[DONE]` marker.

This is a closed-loop capacity gate: enough Locust users are allocated to
approximate the requested completion rate, but it is not a strict open-loop
arrival generator. In streaming mode, 300 RPS means 300 newly completed streams
per second, not 300 simultaneously open streams. Approximate simultaneous
streams as `RPS × average stream duration`; 300 starts/s with 30-second streams
can approach 9,000 open connections. See the
[Locust throughput guidance](https://docs.locust.io/en/stable/writing-a-locustfile.html#wait-time-attribute)
for the closed-model behavior behind this sizing.

Useful overrides include:

```bash
export LOAD_STAGES=25,50,100,200,300
export LOAD_DURATION_SECONDS=180
export LOAD_CHAT_EXPECTED_LATENCY_SECONDS=1.5
export LOAD_STREAM_EXPECTED_LATENCY_SECONDS=8
just load-300
```

The ignored local `docker-compose.load.yml` reuses the development Postgres
volume. `scripts/load-compose.sh` generates it so a clean clone gets the same
profile without tracking the machine-local file. `just load-prod-up` stops the
development stack first; never start it again while the load stack is using the
same volume. Stop the load stack with `just load-prod-down`. Results include the
real provider, network, quota, and current development data; they measure the
end-to-end setup, not the gateway in isolation. The ignored overlay runs three
Uvicorn workers under the application's 3 CPU / 4 GiB cap; Postgres, Redis, the
provider, and the Locust process are outside that budget. Override
`UVICORN_WORKERS` to compare one, two, and three processes without changing the
image.

The gateway keeps the pre-auth inference guard at 120 RPM per IP by default.
An isolated load profile can explicitly raise `INFERENCE_RATE_LIMIT_RPM`; do not
raise it on an Internet-facing deployment unless a trusted ingress supplies the
equivalent flood protection.

## Multi-process / replicas

The production container accepts `UVICORN_WORKERS` from 1 through 32 and
defaults to 1. Each worker is a separate process with its own event loop,
provider clients, memory and Postgres pool; size `DB_POOL_SIZE` and
`DB_MAX_OVERFLOW` against the total worker count. The local load profile defaults
to 3 workers so its three CPU cores can execute Python work concurrently.

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
and starts Postgres, Redis, the Litestar backend, and the Vite frontend. MLflow
is optional and external. Open the admin console at
`http://127.0.0.1:5173/ui/`; API requests are proxied to the backend at
`http://127.0.0.1:8000`. Python changes trigger Litestar reload, while
React/TypeScript/CSS changes use Vite HMR. Stop the stack with `just dev-down`;
volumes are preserved. Changes to dependency manifests or lockfiles require
rebuilding the images.

If filesystem events are not propagated by the Docker host, set
`WATCHFILES_FORCE_POLLING=true` and/or `CHOKIDAR_USEPOLLING=true` before
running `just dev`. Host ports can be changed when a default is occupied, for
example `BACKEND_PORT=18000 FRONTEND_PORT=15173 just dev`; the container
network and frontend API proxy continue to use their fixed internal ports.

For a prod-like compose run on a different host port, set `APP_PORT` (for
example `APP_PORT=18000 docker compose up --build`).
