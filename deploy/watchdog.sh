#!/usr/bin/env bash
# Keeps the aiDocuMines containers running. Run every minute (deploy/install-watchdog.sh
# installs it as a systemd timer); safe to run by hand at any time.
#
# Docker restarts a container whose process exits, but it does NOT:
#   * restart a container that is running and unhealthy (hung worker, stuck web server);
#   * start a container that failed to start once (for example while the server was out
#     of memory) or that was left in "created" / "exited" after the Docker daemon restarted.
# This script does both, and nothing else. It never touches containers it does not list,
# and it leaves alone anything an operator stopped on purpose (see PAUSE below).
#
#   PAUSE:  touch /var/run/aidocumines-watchdog.pause   (remove the file to resume)
set -uo pipefail

PAUSE_FILE="${WATCHDOG_PAUSE_FILE:-/var/run/aidocumines-watchdog.pause}"
[ -e "$PAUSE_FILE" ] && exit 0

# Start order matters: data services first, then the application.
CONTAINERS=(
  aidocumines_api_layer_db
  aidocumines_redis
  aidocumines_milvus
  aidocumines_elasticsearch
  aidocumines_litellm
  aidocumines_web
  aidocumines_celery
  aidocumines_celery_fast
  aidocumines_celery_beat
  aidocumines_file_monitor
  aidocumines_redis_watchdog
  onlyoffice-db
  onlyoffice
)
# A container must be unhealthy on this many consecutive runs before it is restarted,
# so a slow start or one slow health check does not cause a restart loop.
UNHEALTHY_RUNS="${WATCHDOG_UNHEALTHY_RUNS:-3}"
STATE_DIR="${WATCHDOG_STATE_DIR:-/var/tmp/aidocumines-watchdog}"
mkdir -p "$STATE_DIR" 2>/dev/null || STATE_DIR="/tmp"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) watchdog: $*"; }

if ! docker info >/dev/null 2>&1; then
  log "Docker is not responding"
  exit 1
fi

for name in "${CONTAINERS[@]}"; do
  status="$(docker inspect --format '{{.State.Status}}' "$name" 2>/dev/null | tr -d '[:space:]')"
  [ -z "$status" ] && continue   # not deployed on this server

  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null | tr -d '[:space:]')"
  counter="$STATE_DIR/$name.unhealthy"

  case "$status" in
    running)
      if [ "$health" = "unhealthy" ]; then
        n=$(( $(cat "$counter" 2>/dev/null || echo 0) + 1 ))
        echo "$n" > "$counter"
        if [ "$n" -ge "$UNHEALTHY_RUNS" ]; then
          oom="$(docker inspect --format '{{.State.OOMKilled}}' "$name" 2>/dev/null)"
          log "$name unhealthy for $n checks (last OOM-killed: $oom) - restarting"
          docker restart --time 30 "$name" >/dev/null 2>&1 && rm -f "$counter"
        fi
      else
        rm -f "$counter"
      fi
      ;;
    exited|created|dead)
      oom="$(docker inspect --format '{{.State.OOMKilled}}' "$name" 2>/dev/null)"
      code="$(docker inspect --format '{{.State.ExitCode}}' "$name" 2>/dev/null)"
      log "$name is $status (exit code $code, OOM-killed: $oom) - starting"
      docker start "$name" >/dev/null 2>&1 || log "could not start $name"
      ;;
    restarting|paused|removing)
      ;;  # Docker is already handling it, or an operator paused it
  esac
done

# Disk: a full disk stops the database and Redis from writing. Warn early.
usage="$(df -P / 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')"
if [ -n "${usage:-}" ] && [ "$usage" -ge "${WATCHDOG_DISK_WARN:-90}" ]; then
  log "WARNING: root disk is ${usage}% full"
fi
exit 0
