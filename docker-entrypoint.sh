#!/bin/sh
set -e

# Apply pending migrations before serving. Idempotent (a no-op when the schema is
# already at head), so it is safe on every container start / restart.
litestar --app litestar_test.app:app database upgrade --no-prompt

exec uvicorn litestar_test.app:app \
  --host 0.0.0.0 --port 8000 \
  --proxy-headers --forwarded-allow-ips "*"
