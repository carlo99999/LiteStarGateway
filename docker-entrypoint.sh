#!/bin/sh
set -e

# Apply pending migrations before serving. Idempotent (a no-op when the schema is
# already at head) — fine for a single instance. With multiple replicas, run the
# migration as a dedicated one-shot job / init container instead and set
# MIGRATE_ON_START=false here, so N replicas don't race the same upgrade:
#   docker run --rm <image> litestar --app litestar_gateway.app:app database upgrade --no-prompt
if [ "${MIGRATE_ON_START:-true}" = "true" ]; then
  litestar --app litestar_gateway.app:app database upgrade --no-prompt
fi

# Uvicorn defaults to one worker regardless of the container CPU quota. Keep the
# production default conservative, but allow operators and the load profile to
# opt into process-level CPU parallelism. Reject typos before Uvicorn can fork an
# unexpectedly large process fleet.
uvicorn_workers=${UVICORN_WORKERS-1}
case "$uvicorn_workers" in
  ""|*[!0-9]*)
    echo "error: UVICORN_WORKERS must be an integer from 1 to 32" >&2
    exit 1
    ;;
esac
if [ "$uvicorn_workers" -lt 1 ] || [ "$uvicorn_workers" -gt 32 ]; then
  echo "error: UVICORN_WORKERS must be an integer from 1 to 32" >&2
  exit 1
fi

# Trust X-Forwarded-For/-Proto only from FORWARDED_ALLOW_IPS (the reverse proxy's
# IP/CIDR). The default is loopback, so forwarded headers from arbitrary peers are
# ignored — a "*" default would let any direct client forge a fresh client IP per
# request, bypassing the per-IP auth rate limit and spoofing the audit log.
exec uvicorn litestar_gateway.app:app \
  --host 0.0.0.0 --port 8000 \
  --workers "$uvicorn_workers" \
  --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
