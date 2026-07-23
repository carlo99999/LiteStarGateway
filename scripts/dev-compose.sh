#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE_INPUT=${DEV_ENV_FILE:-"$ROOT/.env.docker-dev"}
ENV_FILE_DIRECTORY=$(CDPATH= cd -- "$(dirname -- "$ENV_FILE_INPUT")" && pwd)
ENV_FILE="$ENV_FILE_DIRECTORY/$(basename -- "$ENV_FILE_INPUT")"
POSTGRES_VOLUME=litestar-gateway-dev_pg-dev-data

case "$ENV_FILE" in
  "$ROOT/.env.docker-dev") ;;
  "$ROOT"/*)
    echo "error: a custom DEV_ENV_FILE must be outside the Docker build context" >&2
    exit 1
    ;;
esac

generate_secret() {
  openssl rand -hex 32
}

create_env_file() {
  if ! command -v openssl >/dev/null 2>&1; then
    echo "error: openssl is required to generate local development secrets" >&2
    exit 1
  fi

  umask 077
  temporary_file=$(mktemp "${ENV_FILE}.tmp.XXXXXX")
  trap 'rm -f "$temporary_file"' EXIT HUP INT TERM

  {
    echo "POSTGRES_PASSWORD=$(generate_secret)"
    echo "MASTER_KEY=$(generate_secret)"
    echo "JWT_SECRET=$(generate_secret)"
    echo "SALT_KEY=$(generate_secret)"
  } >"$temporary_file"

  mv "$temporary_file" "$ENV_FILE"
  trap - EXIT HUP INT TERM
  echo "Created $ENV_FILE with random local development secrets."
}

require_value() {
  key=$1
  count=$(grep -Ec "^${key}=" "$ENV_FILE" || true)
  if [ "$count" -ne 1 ]; then
    echo "error: $ENV_FILE must define $key exactly once" >&2
    exit 1
  fi
}

read_value() {
  key=$1
  sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1
}

require_safe_value() {
  key=$1
  value=$(read_value "$key")
  case "$value" in
    "" | *[!A-Za-z0-9._~-]*)
      echo "error: $key in $ENV_FILE must contain only URL-safe characters" >&2
      exit 1
      ;;
  esac
}

require_strong_secret() {
  key=$1
  value=$(read_value "$key")
  if [ "${#value}" -lt 32 ]; then
    echo "error: $key in $ENV_FILE must contain at least 32 characters" >&2
    exit 1
  fi
}

if [ -L "$ENV_FILE" ]; then
  echo "error: $ENV_FILE must not be a symbolic link" >&2
  exit 1
elif [ ! -e "$ENV_FILE" ]; then
  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "error: Docker must be running before local secrets can be initialized" >&2
    exit 1
  fi
  if docker volume inspect "$POSTGRES_VOLUME" >/dev/null 2>&1; then
    echo "error: $ENV_FILE is missing but the existing Postgres volume still uses its password" >&2
    echo "restore the env file or explicitly remove $POSTGRES_VOLUME to reset local data" >&2
    exit 1
  fi
  create_env_file
elif [ ! -f "$ENV_FILE" ]; then
  echo "error: $ENV_FILE exists but is not a regular file" >&2
  exit 1
fi

for key in POSTGRES_PASSWORD MASTER_KEY JWT_SECRET SALT_KEY; do
  require_value "$key"
  require_safe_value "$key"
done
for key in MASTER_KEY JWT_SECRET SALT_KEY; do
  require_strong_secret "$key"
done
chmod 600 "$ENV_FILE"

DOCKER_RUNTIME_PLATFORM=${DOCKER_DEFAULT_PLATFORM:-}
if [ -z "$DOCKER_RUNTIME_PLATFORM" ]; then
  DOCKER_RUNTIME_PLATFORM=$(docker info --format '{{.OSType}}-{{.Architecture}}' 2>/dev/null || true)
fi
if [ -z "$DOCKER_RUNTIME_PLATFORM" ]; then
  DOCKER_RUNTIME_PLATFORM=unknown
fi
UI_DEPENDENCY_FINGERPRINT=$(
  {
    cksum \
      "$ROOT/ui/package.json" \
      "$ROOT/ui/pnpm-lock.yaml" \
      "$ROOT/ui/pnpm-workspace.yaml" \
      "$ROOT/Dockerfile.dev"
    printf '%s\n' "$DOCKER_RUNTIME_PLATFORM"
  } |
    cksum |
    awk '{print $1}'
)
UI_NODE_MODULES_VOLUME="litestar-gateway-dev_ui-dev-node-modules-$UI_DEPENDENCY_FINGERPRINT"

if [ "$#" -eq 0 ]; then
  set -- up --build
fi

exec env \
  -u POSTGRES_PASSWORD \
  -u MASTER_KEY \
  -u JWT_SECRET \
  -u SALT_KEY \
  UI_NODE_MODULES_VOLUME="$UI_NODE_MODULES_VOLUME" \
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.dev.yml" "$@"
