#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env.benchmark"
COMPOSE_FILE="$ROOT_DIR/docker-compose.benchmark.yml"
PROJECT_NAME="${BENCHMARK_PROJECT_NAME:-litestar-gateway-benchmark}"

ensure_environment() {
  umask 077
  if [[ -L "$ENV_FILE" || (-e "$ENV_FILE" && ! -f "$ENV_FILE") ]]; then
    echo "Refusing unsafe benchmark environment path: $ENV_FILE" >&2
    exit 2
  fi
  if [[ ! -f "$ENV_FILE" ]]; then
    local temporary
    temporary="$(mktemp "$ROOT_DIR/.env.benchmark.tmp.XXXXXX")"
    trap 'rm -f "$temporary"' RETURN
    {
      printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 32)"
      printf 'MASTER_KEY=%s\n' "$(openssl rand -hex 32)"
      printf 'JWT_SECRET=%s\n' "$(openssl rand -hex 32)"
      printf 'SALT_KEY=%s\n' "$(openssl rand -hex 32)"
      printf 'ADMIN_EMAIL=benchmark-admin@example.invalid\n'
    } >"$temporary"
    chmod 600 "$temporary"
    mv "$temporary" "$ENV_FILE"
    trap - RETURN
  fi
  chmod 600 "$ENV_FILE"
  local key value
  while IFS='=' read -r key value; do
    case "$key" in
      POSTGRES_PASSWORD|MASTER_KEY|JWT_SECRET|SALT_KEY)
        if [[ ! "$value" =~ ^[0-9a-f]{64}$ ]]; then
          echo "Invalid $key in $ENV_FILE; expected 64 lowercase hex characters" >&2
          exit 2
        fi
        ;;
      ADMIN_EMAIL)
        if [[ "$value" != "benchmark-admin@example.invalid" ]]; then
          echo "Invalid ADMIN_EMAIL in $ENV_FILE" >&2
          exit 2
        fi
        ;;
      "")
        continue
        ;;
      *)
        echo "Unexpected key $key in $ENV_FILE" >&2
        exit 2
        ;;
    esac
    printf -v "$key" '%s' "$value"
    export "$key"
  done <"$ENV_FILE"
  for key in POSTGRES_PASSWORD MASTER_KEY JWT_SECRET SALT_KEY ADMIN_EMAIL; do
    if [[ -z "${!key:-}" ]]; then
      echo "Missing $key in $ENV_FILE" >&2
      exit 2
    fi
  done
}

compose() {
  docker compose --project-name "$PROJECT_NAME" --file "$COMPOSE_FILE" "$@"
}

up() {
  ensure_environment
  export UVICORN_WORKERS="${UVICORN_WORKERS:-3}"
  export DB_POOL_SIZE="${DB_POOL_SIZE:-5}"
  export DB_MAX_OVERFLOW="${DB_MAX_OVERFLOW:-0}"
  export LOAD_MOCK_TTFT_MS="${LOAD_MOCK_TTFT_MS:-25}"
  export LOAD_MOCK_CHUNK_INTERVAL_MS="${LOAD_MOCK_CHUNK_INTERVAL_MS:-10}"
  export LOAD_MOCK_TOTAL_LATENCY_MS="${LOAD_MOCK_TOTAL_LATENCY_MS:-50}"
  export LOAD_MOCK_CHUNK_COUNT="${LOAD_MOCK_CHUNK_COUNT:-2}"
  export LOAD_MOCK_FAILURE_EVERY="${LOAD_MOCK_FAILURE_EVERY:-0}"
  export LOAD_MOCK_FAILURE_STATUS="${LOAD_MOCK_FAILURE_STATUS:-503}"
  compose up --detach --build --wait
}

down() {
  ensure_environment
  compose down --volumes --remove-orphans
}

run_contract() {
  down
  up
  trap down EXIT INT TERM
  local app_container mock_container
  app_container="$(compose ps --quiet app)"
  mock_container="$(compose ps --quiet mock)"
  if [[ -z "$app_container" || -z "$mock_container" ]]; then
    echo "Benchmark containers are not running" >&2
    exit 2
  fi
  export LOAD_HOST="http://127.0.0.1:${BENCHMARK_PORT:-18000}"
  export LOAD_MODES="${LOAD_MODES:-chat,chat-stream}"
  export LOAD_PROFILE_POLICY="${LOAD_PROFILE_POLICY:-fail-fast}"
  export LOAD_STAGES="${LOAD_STAGES:-25,50,100,150,200,250,300}"
  export LOAD_CHAT_EXPECTED_LATENCY_SECONDS="${LOAD_CHAT_EXPECTED_LATENCY_SECONDS:-1}"
  export LOAD_STREAM_EXPECTED_LATENCY_SECONDS="${LOAD_STREAM_EXPECTED_LATENCY_SECONDS:-3}"
  export LOAD_DURATION_SECONDS="${LOAD_DURATION_SECONDS:-60}"
  export LOAD_RAMP_SECONDS="${LOAD_RAMP_SECONDS:-10}"
  export LOAD_SETTLE_SECONDS="${LOAD_SETTLE_SECONDS:-5}"
  export LOAD_USER_HEADROOM="${LOAD_USER_HEADROOM:-1.25}"
  export LOAD_MAX_TOKENS="${LOAD_MAX_TOKENS:-8}"
  export LOAD_MIN_RPS_RATIO="${LOAD_MIN_RPS_RATIO:-0.95}"
  export LOAD_MAX_FAILURE_RATIO="${LOAD_MAX_FAILURE_RATIO:-0.001}"
  export LOAD_CHAT_MAX_P95_MS="${LOAD_CHAT_MAX_P95_MS:-500}"
  export LOAD_STREAM_MAX_P95_MS="${LOAD_STREAM_MAX_P95_MS:-750}"
  export LOAD_STREAM_MAX_TTFT_MS="${LOAD_STREAM_MAX_TTFT_MS:-750}"
  export LOAD_RESOURCE_CONTAINERS
  LOAD_RESOURCE_CONTAINERS="$(
    printf '{"gateway":"%s","mock":"%s"}' "$app_container" "$mock_container"
  )"
  uv sync --locked --group load
  uv run --locked --no-sync --group load python scripts/run_benchmark_contract.py
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  run) run_contract ;;
  *)
    echo "Usage: $0 {up|down|run}" >&2
    exit 2
    ;;
esac
