#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE="$ROOT/.env.docker-dev"
OVERLAY_FILE="$ROOT/docker-compose.load.yml"
DEV_VOLUME=litestar-gateway-dev_pg-dev-data

create_overlay() {
  if [ -e "$OVERLAY_FILE" ]; then
    if [ ! -f "$OVERLAY_FILE" ] || [ -L "$OVERLAY_FILE" ]; then
      echo "error: $OVERLAY_FILE must be a regular file" >&2
      exit 1
    fi
    return
  fi

  temporary_file=$(mktemp "$ROOT/.docker-compose.load.tmp.XXXXXX")
  trap 'rm -f "$temporary_file"' EXIT HUP INT TERM
  {
    echo "# Generated local-only load profile. This file is gitignored."
    echo "services:"
    echo "  db:"
    echo "    volumes:"
    echo "      - dev-pg-data:/var/lib/postgresql/data"
    echo "  app:"
    echo "    environment:"
    echo '      INFERENCE_RATE_LIMIT_RPM: ${INFERENCE_RATE_LIMIT_RPM:-36000}'
    echo '      MAX_RETRIES: ${MAX_RETRIES:-0}'
    echo '      UVICORN_WORKERS: ${UVICORN_WORKERS:-3}'
    echo "volumes:"
    echo "  dev-pg-data:"
    echo "    external: true"
    echo "    name: $DEV_VOLUME"
  } >"$temporary_file"
  mv "$temporary_file" "$OVERLAY_FILE"
  trap - EXIT HUP INT TERM
  echo "Created ignored load overlay: $OVERLAY_FILE"
}

require_existing_dev_state() {
  if [ ! -f "$ENV_FILE" ]; then
    echo "error: $ENV_FILE is missing; run 'just dev' once to initialize local state" >&2
    exit 1
  fi
  if ! docker volume inspect "$DEV_VOLUME" >/dev/null 2>&1; then
    echo "error: $DEV_VOLUME is missing; run 'just dev' once to create and seed it" >&2
    exit 1
  fi
}

compose() {
  env \
    -u POSTGRES_PASSWORD \
    -u MASTER_KEY \
    -u JWT_SECRET \
    -u SALT_KEY \
    docker compose \
      --env-file "$ENV_FILE" \
      -f "$ROOT/docker-compose.yml" \
      -f "$OVERLAY_FILE" \
      "$@"
}

command=${1:-up}
case "$command" in
  init)
    create_overlay
    ;;
  up)
    create_overlay
    require_existing_dev_state
    "$ROOT/scripts/dev-compose.sh" down
    compose up --build -d
    ;;
  down)
    create_overlay
    require_existing_dev_state
    compose down
    ;;
  config)
    create_overlay
    require_existing_dev_state
    compose config
    ;;
  *)
    echo "usage: $0 [init|up|down|config]" >&2
    exit 2
    ;;
esac
