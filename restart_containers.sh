#!/usr/bin/env bash
#
# restart_containers.sh
#
# Restarts application containers (web, celery) that run our code.
# Infrastructure containers (Milvus, ES, Redis, DB) have restart policies
# and only get restarted if they are UNHEALTHY.
#
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

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

    # Check if container has a healthcheck
    health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null | tr -d '[:space:]' || true)
    [ -n "$health" ] || health="none"

    if [ "$status" = "running" ] && [ "$health" = "healthy" ]; then
        echo -e "${GREEN}✓  $name is healthy — skipping restart${NC}"
    elif [ "$status" = "running" ] && [ "$health" = "none" ]; then
        echo -e "${GREEN}✓  $name is running (no healthcheck) — skipping restart${NC}"
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

# ──────────────────────────────────────────────────────────
# Always restart app containers (they run our code)
# ──────────────────────────────────────────────────────────
echo "── Application containers ──"
for container in aidocumines_web aidocumines_celery aidocumines_celery_beat; do
    status=$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null | tr -d '[:space:]' || true)
    if [ -z "$status" ]; then
        echo -e "${YELLOW}⚠  $container does not exist — start it with: docker compose up -d --no-deps ${container#aidocumines_}${NC}"
    else
        echo -e "${YELLOW}⟳  Restarting $container${NC}"
        if docker restart "$container" >/dev/null 2>&1; then
            echo -e "${GREEN}✓  $container restarted${NC}"
        else
            echo -e "${RED}✗  failed to restart $container — continuing${NC}"
        fi
    fi
done

# ──────────────────────────────────────────────────────────
# Restart infrastructure only if unhealthy
# ──────────────────────────────────────────────────────────
echo ""
echo "── Infrastructure containers ──"
# Only aiDocuMines' own containers (never CapRover srv-* ones, which CapRover replaces itself)
for container in aidocumines_api_layer_db aidocumines_redis aidocumines_milvus_etcd aidocumines_milvus_minio aidocumines_milvus aidocumines_elasticsearch aidocumines_litellm; do
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
