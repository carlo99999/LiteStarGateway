# Design doc — Container image & deployment

> **Status:** Draft / parked (pre-v1). Branch `adding-docker-deploy`. No code yet.

## 1. Goal

Make the gateway actually runnable in production: a container image and a
documented way to serve it. Today there's only `uv run litestar ... run` (dev
server), which is not a production setup.

## 2. Plan

- **Dockerfile** (multi-stage): build stage installs deps with `uv` from
  `uv.lock` (reproducible); slim runtime stage copies the venv + source. Non-root
  user. `.dockerignore` to keep the image lean.
- **ASGI server**: run under **uvicorn** (or gunicorn+uvicorn workers) with
  multiple workers, `--proxy-headers`, `--forwarded-allow-ips` so the real client
  IP reaches the app — **required** for the per-IP rate limiting to be meaningful.
- **TLS**: terminate at a reverse proxy / ingress; document that the app expects
  to sit behind one.
- **Config**: all via env (already the case). Document required vars
  (`DATABASE_URL` → Postgres, `MASTER_KEY`, `JWT_SECRET`, `SALT_KEY`,
  `ENVIRONMENT=production`).
- **Health/readiness**: keep `/health`; add a readiness check that verifies DB
  connectivity for orchestrator probes.
- **Docs**: a "Deploy" section in the README + an example `docker-compose.yml`
  (app + Postgres) for local prod-like runs.

## 3. Multi-process implications (call out)

Rate-limit and any future cache use an in-memory store per process. With multiple
workers, back them with a **shared store (Redis)** or limits are per-worker. Tie
this to the rate-limit/observability docs.

## 4. Open decisions

1. **uvicorn vs gunicorn+uvicorn workers** (process management).
2. **Base image**: `python:3.x-slim` vs distroless.
3. **Migrations on deploy**: run `alembic upgrade head` as an init step (depends
   on `adding-db-migrations`).
4. **Redis** as a dependency now vs later (needed for multi-worker rate limiting).

## 5. Rollout

1. `feat/dockerfile` — Dockerfile + .dockerignore + README deploy section.
2. `feat/compose-and-readiness` — docker-compose (app+Postgres) + readiness probe.
