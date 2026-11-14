#!/usr/bin/env bash
#
# restart_containers.sh
#
# Run after `git pull` on the server.
#   ./restart_containers.sh               apply the compose file, run migrations, restart the app
#   ./restart_containers.sh --purge-queue also empty the task queues first (use when jobs are
#                                         stuck: old tasks survive restarts because the queue
#                                         is stored on disk; their work is marked Failed so
#                                         people can simply run it again)
#   ./restart_containers.sh --restart-only  old behaviour: just `docker restart` the app
#
# Infrastructure containers (database, Redis, Milvus, Elasticsearch) are only recreated when
# their settings in docker-compose.yml changed, and only restarted when unhealthy.
#
set -euo pipefail
cd "$(dirname "$0")"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PURGE=0
RESTART_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --purge-queue) PURGE=1 ;;
    --restart-only) RESTART_ONLY=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

APP_CONTAINERS=(aidocumines_web aidocumines_celery aidocumines_celery_fast aidocumines_celery_beat aidocumines_file_monitor)
INFRA_CONTAINERS=(aidocumines_api_layer_db aidocumines_redis aidocumines_milvus aidocumines_elasticsearch aidocumines_litellm)

# ──────────────────────────────────────────────────────────
# Preflight: refuse to restart into a configuration that cannot start
# ──────────────────────────────────────────────────────────
env_value() { grep -E "^[[:space:]]*$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"'"'"'\r' | sed 's/[[:space:]]*#.*$//;s/[[:space:]]*$//'; }

if [ ! -f .env ]; then
  echo -e "${RED}✗  .env not found in $(pwd) — nothing was changed${NC}"; exit 1
fi
missing=0
for var in SECRET_KEY POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB FIELD_ENCRYPTION_KEY ENCRYPTION_SECRET_KEY REDIS_PASSWORD ELASTICSEARCH_PASSWORD; do
  if [ -z "$(env_value "$var")" ]; then
    echo -e "${RED}✗  $var is not set in .env${NC}"; missing=1
  fi
done
case "$(env_value SECRET_KEY)" in
  django-insecure-default|changeme|change-me|secret)
    echo -e "${RED}✗  SECRET_KEY in .env is a placeholder; generate one: python3 -c \"import secrets; print(secrets.token_urlsafe(64))\"${NC}"; missing=1 ;;
esac
if [ "$missing" -ne 0 ]; then
  echo -e "${RED}Nothing was restarted. Add the values above to .env and run again.${NC}"; exit 1
fi
case "$(env_value DEBUG | tr '[:upper:]' '[:lower:]')" in
  true|1|yes) echo -e "${YELLOW}⚠  DEBUG is on in .env — turn it off on a public server (DEBUG=False)${NC}" ;;
esac
[ -z "$(env_value REDIS_PASSWORD)" ] && echo -e "${YELLOW}⚠  REDIS_PASSWORD is not set in .env — the task queue has no password${NC}"

# ──────────────────────────────────────────────────────────
# Helper: restart a container only if it is unhealthy or stopped
# ──────────────────────────────────────────────────────────
restart_if_needed() {
    local name="$1"
    local status health

    # Trim whitespace: `docker inspect` emits a blank line for a missing object
    # even when stderr is suppressed, so a raw compare against "missing" fails.
    status=$(docker inspect --format '{{.State.Status}}' "$name" 2>/dev/null | tr -d '[:space:]' || true)
    if [ -z "$status" ] || [ "$status" = "missing" ]; then
        echo -e "${YELLOW}⚠  $name does not exist — skipping${NC}"
        return 0
    fi

    health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null | tr -d '[:space:]' || true)
    [ -n "$health" ] || health="none"

    if [ "$status" = "running" ] && { [ "$health" = "healthy" ] || [ "$health" = "none" ] || [ "$health" = "starting" ]; }; then
        echo -e "${GREEN}✓  $name is $status / $health${NC}"
    else
        echo -e "${YELLOW}⟳  $name is $status / $health — restarting${NC}"
        if docker restart "$name" >/dev/null 2>&1; then
            echo -e "${GREEN}✓  $name restarted${NC}"
        else
            echo -e "${RED}✗  failed to restart $name — continuing${NC}"
        fi
    fi
    return 0
}

restart_app_containers() {
  for container in "${APP_CONTAINERS[@]}"; do
    status=$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null | tr -d '[:space:]' || true)
    if [ -z "$status" ]; then
      echo -e "${YELLOW}⚠  $container does not exist — it is created by: docker compose up -d${NC}"
    else
      echo -e "${YELLOW}⟳  Restarting $container${NC}"
      docker restart "$container" >/dev/null 2>&1 \
        && echo -e "${GREEN}✓  $container restarted${NC}" \
        || echo -e "${RED}✗  failed to restart $container — continuing${NC}"
    fi
  done
}

# The compose project the running containers belong to (so `up` updates them in place
# instead of colliding with them under a different project name).
PROJECT="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' aidocumines_web 2>/dev/null | tr -d '[:space:]' || true)"
COMPOSE=(docker compose -f docker-compose.yml)
[ -n "$PROJECT" ] && COMPOSE=(docker compose -p "$PROJECT" -f docker-compose.yml)

# ──────────────────────────────────────────────────────────
# Optional: empty the task queues before the workers come back
# ──────────────────────────────────────────────────────────
if [ "$PURGE" -eq 1 ]; then
  echo "── Task queues ──"
  for worker in aidocumines_celery aidocumines_celery_fast; do
    docker stop --time 20 "$worker" >/dev/null 2>&1 || true
  done
  if docker exec aidocumines_web python manage.py queue_doctor --purge 2>/dev/null | tail -3; then
    echo -e "${GREEN}✓  queues emptied; unfinished work marked Failed${NC}"
  else
    echo -e "${RED}✗  could not purge through the web container — run after it is up: docker exec aidocumines_web python manage.py queue_doctor --purge${NC}"
  fi
fi

# ──────────────────────────────────────────────────────────
# Apply the compose file (memory caps, health checks, new services), migrate, restart
# ──────────────────────────────────────────────────────────
echo "── Application containers ──"
if [ "$RESTART_ONLY" -eq 1 ]; then
  restart_app_containers
else
  # Build first: the database runs from a locally built TLS image (deploy/Dockerfile.postgres) and the
  # application image must match the pulled code. Layers are cached, so this is quick unless
  # production_requirements.txt changed.
  if ! "${COMPOSE[@]}" build; then
    echo -e "${RED}✗  'docker compose build' failed (see above). Nothing was restarted: the running containers keep${NC}"
    echo -e "${RED}   serving. Do NOT restart them by hand — the pulled code expects the new database TLS settings.${NC}"
    exit 1
  fi
  if "${COMPOSE[@]}" up -d --remove-orphans; then
    echo -e "${GREEN}✓  compose file applied (migrations ran in db_prepare)${NC}"
    # Containers whose settings did not change were left running: restart them for the new code.
    restart_app_containers
  else
    echo -e "${RED}✗  'docker compose up' failed (see the message above). Check: docker compose ps -a; docker logs aidocumines_api_layer_db${NC}"
    exit 1
  fi
fi

echo ""
echo "── Infrastructure containers ──"
# Only aiDocuMines' own containers (never CapRover srv-* ones, which CapRover replaces itself)
for container in "${INFRA_CONTAINERS[@]}"; do
    restart_if_needed "$container"
done

# ──────────────────────────────────────────────────────────
# Reapply CORS fix to the CapRover Swarm proxy container
# ──────────────────────────────────────────────────────────
echo ""
PROXY_CONTAINER=$(docker ps --filter name=srv-captain--aidocumines-api-layer --format "{{.ID}}" 2>/dev/null | head -1 || true)
if [ -n "$PROXY_CONTAINER" ]; then
    docker cp /home/aidocumines/Apps/api-layer/nginx.conf "$PROXY_CONTAINER":/etc/nginx/conf.d/default.conf 2>/dev/null || true
    docker exec "$PROXY_CONTAINER" nginx -s reload 2>/dev/null || true
    echo -e "${GREEN}✓  CORS fix applied to proxy container: $PROXY_CONTAINER${NC}"
fi

echo ""
if ! systemctl is-enabled aidocumines-watchdog.timer >/dev/null 2>&1; then
  echo -e "${YELLOW}⚠  The container watchdog is not installed. Run once: sudo ./deploy/install-watchdog.sh${NC}"
fi
echo "Queue status:  docker exec aidocumines_web python manage.py queue_doctor"
