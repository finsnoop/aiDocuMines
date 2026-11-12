#!/usr/bin/env bash
# Build and start the whole aiDocuMines platform (API layer + Node backend + frontend).
#
#   ./deploy/platform-up.sh          build + start everything
#   ./deploy/platform-up.sh down     stop everything
#   ./deploy/platform-up.sh logs     follow logs
#
# Expects the three repos side by side (override with BACKEND_REPO / FRONTEND_REPO)
# and a .env in aiDocuMines and aiDocuMines-Backend.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export BACKEND_REPO="${BACKEND_REPO:-$(cd "$ROOT/../aiDocuMines-Backend" && pwd)}"
export FRONTEND_REPO="${FRONTEND_REPO:-$(cd "$ROOT/../aiDocuMines-Frontend" && pwd)}"
export BACKEND_ENV_FILE="${BACKEND_ENV_FILE:-$BACKEND_REPO/.env}"

COMPOSE=(docker compose -p aidocumines -f docker-compose.yml -f deploy/docker-compose.platform.yml)

case "${1:-up}" in
  down) exec "${COMPOSE[@]}" down ;;
  logs) exec "${COMPOSE[@]}" logs -f --tail 100 ;;
esac

for f in "$ROOT/.env" "$BACKEND_ENV_FILE"; do
  [ -f "$f" ] || { echo "Missing $f - copy it from your secrets store before starting." >&2; exit 1; }
done

docker network inspect captain-overlay-network >/dev/null 2>&1 \
  || docker network create --attachable captain-overlay-network >/dev/null

"${COMPOSE[@]}" up -d --build

cat <<EOF

aiDocuMines is starting:
  Frontend      http://localhost:${FRONTEND_PORT:-4300}
  API layer     http://localhost:${API_PORT:-8020}/api/v1/swagger/
  Node backend  http://localhost:${BACKEND_PORT:-7000}/api/db-health
  Logs: ./deploy/platform-up.sh logs     Stop: ./deploy/platform-up.sh down
EOF
