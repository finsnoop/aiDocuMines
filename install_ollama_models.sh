#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-aidocumines_ollama}"
OLLAMA_HOST_URL="${OLLAMA_HOST_URL:-http://127.0.0.1:11435}"   # docker-compose.yml publishes 127.0.0.1:11435
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-180}"

MODELS=(
  "qwen2.5:0.5b-instruct"
  "qwen"
  "phi4"
  "gpt-oss:20b"
)

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
fi

log() { echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "Missing required command: $1"; exit 1; }
}

require_cmd docker
require_cmd curl

container_running() {
  docker ps --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"
}

container_healthy() {
  local status
  status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER_NAME" 2>/dev/null || true)"
  [[ "$status" == "healthy" ]] || [[ "$status" == "none" ]]
}

wait_for_container() {
  log "Waiting for container '$CONTAINER_NAME' to be running..."
  local start ts
  start="$(date +%s)"
  while ! container_running; do
    ts="$(date +%s)"
    if (( ts - start > HEALTH_TIMEOUT_SECONDS )); then
      echo "ERROR: Container '$CONTAINER_NAME' not running after ${HEALTH_TIMEOUT_SECONDS}s"
      exit 1
    fi
    sleep 2
  done
  log "Container '$CONTAINER_NAME' is running."
}

wait_for_health() {
  log "Waiting for container '$CONTAINER_NAME' to be healthy (or no healthcheck)..."
  local start ts
  start="$(date +%s)"
  while ! container_healthy; do
    ts="$(date +%s)"
    if (( ts - start > HEALTH_TIMEOUT_SECONDS )); then
      echo "ERROR: Container '$CONTAINER_NAME' not healthy after ${HEALTH_TIMEOUT_SECONDS}s"
      docker inspect "$CONTAINER_NAME" --format '{{json .State.Health}}' 2>/dev/null || true
      exit 1
    fi
    sleep 2
  done
  log "Container health OK."
}

wait_for_api() {
  log "Waiting for Ollama API at $OLLAMA_HOST_URL ..."
  local start ts
  start="$(date +%s)"
  while true; do
    if curl -fsS "$OLLAMA_HOST_URL/api/tags" >/dev/null 2>&1; then
      log "Ollama API is responding."
      return 0
    fi
    ts="$(date +%s)"
    if (( ts - start > HEALTH_TIMEOUT_SECONDS )); then
      echo "ERROR: Ollama API not responding after ${HEALTH_TIMEOUT_SECONDS}s at $OLLAMA_HOST_URL"
      exit 1
    fi
    sleep 2
  done
}

model_installed() {
  local m="$1"
  docker exec -i "$CONTAINER_NAME" sh -lc "ollama list | awk 'NR>1 {print \$1}' | grep -Fxq '$m'"
}

pull_model() {
  local m="$1"
  log "Pulling model: $m"
  docker exec -i "$CONTAINER_NAME" sh -lc "ollama pull '$m'"
  log "Done: $m"
}

main() {
  wait_for_container
  wait_for_health
  wait_for_api

  log "Installing models into '$CONTAINER_NAME'..."
  for m in "${MODELS[@]}"; do
    if (( FORCE == 0 )) && model_installed "$m"; then
      log "Already installed (skipping): $m"
      continue
    fi
    pull_model "$m"
  done

  log "Final installed models:"
  docker exec -i "$CONTAINER_NAME" sh -lc "ollama list"

  log "✅ Ollama models ready."
}

main

