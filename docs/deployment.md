# Design doc — Container image & deployment

> **Status:** Implemented — see `Dockerfile`, `docker-compose.yml`,
> `docker-entrypoint.sh`, `.dockerignore`. Retained as the original design
> rationale.

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
- **Health/readiness**: `/health` is a liveness-only probe (it does not touch the
  database); the container `HEALTHCHECK` calls it. No DB-connectivity readiness
  probe was added.
- **Docs**: a "Deploy" section in the README + an example `docker-compose.yml`
  (app + Postgres) for local prod-like runs.

## 3. Multi-process implications (call out)

Rate-limit and any future cache use an in-memory store per process. With multiple
workers, back them with a **shared store (Redis)** or limits are per-worker. Tie
this to the rate-limit/observability docs.

## 4. Decisions (as implemented)

1. **ASGI server**: **uvicorn** with `--proxy-headers --forwarded-allow-ips`
   (`docker-entrypoint.sh`).
2. **Base image**: builder `ghcr.io/astral-sh/uv:python3.14-bookworm-slim`,
   runtime `python:3.14-slim-bookworm` (`Dockerfile`).
3. **Migrations on deploy**: run on start via
   `litestar … database upgrade --no-prompt` in `docker-entrypoint.sh` (gated by
   `MIGRATE_ON_START`, default on).
4. **Redis**: shipped as a `docker-compose.yml` service and enabled via
   `REDIS_URL` (needed for multi-worker rate limiting; falls back to in-memory
   when unset).

## 5. Rollout

1. `feat/dockerfile` — Dockerfile + .dockerignore + README deploy section.
2. `feat/compose-and-readiness` — docker-compose (app+Postgres) + readiness probe.
