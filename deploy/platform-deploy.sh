#!/usr/bin/env bash
# Server-side deploy of the API layer stack, called by the Platform CI/CD pipeline:
#   ./deploy/platform-deploy.sh <commit-sha>
#
# Checks out the exact commit that passed CI, rebuilds the application containers,
# runs migrations (via the db_prepare service) and waits for the health check.
# If the new version is unhealthy it rolls back to the previously running commit.
set -euo pipefail

SHA="${1:?usage: platform-deploy.sh <commit-sha>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APP_SERVICES=(web celery celery_beat file_monitor)
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8020/api/v1/health/}"
COMPOSE=(docker compose -f docker-compose.yml)

PREVIOUS="$(git rev-parse HEAD)"
echo "Deploying ${SHA} (currently ${PREVIOUS})"

git fetch --quiet origin
git checkout --quiet --detach "$SHA"

deploy() {
  "${COMPOSE[@]}" build "${APP_SERVICES[@]}" db_prepare
  "${COMPOSE[@]}" run --rm db_prepare
  "${COMPOSE[@]}" up -d --no-deps "${APP_SERVICES[@]}"
}

healthy() {
  for _ in $(seq 1 30); do
    if docker exec aidocumines_web curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  return 1
}

deploy
if healthy; then
  echo "API layer healthy at ${SHA}"
  exit 0
fi

echo "API layer unhealthy after deploy; rolling back to ${PREVIOUS}" >&2
git checkout --quiet --detach "$PREVIOUS"
deploy
healthy && echo "Rolled back to ${PREVIOUS}" >&2
exit 1
