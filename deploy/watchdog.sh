#!/usr/bin/env bash
# Keeps the aiDocuMines containers running. Run every minute (deploy/install-watchdog.sh
# installs it as a systemd timer); safe to run by hand at any time.
#
# Docker restarts a container whose process exits, but it does NOT:
#   * restart a container that is running and unhealthy (hung worker, stuck web server);
#   * start a container that failed to start once (for example while the server was out
#     of memory) or that was left in "created" / "exited" after the Docker daemon restarted.
# This script does both, and nothing else. It never touches containers it does not list.
# It cannot distinguish a manual stop from a failed container: use PAUSE below during
# maintenance to stop it from starting exited containers. Individually paused containers
# are also left alone.
#
#   PAUSE:  touch /var/run/aidocumines-watchdog.pause   (remove the file to resume)
set -uo pipefail

PAUSE_FILE="${WATCHDOG_PAUSE_FILE:-/var/run/aidocumines-watchdog.pause}"
[ -e "$PAUSE_FILE" ] && exit 0

# Start order matters: data services first, then the application.
CONTAINERS=(
  aidocumines_api_layer_db
  aidocumines_redis
  aidocumines_milvus_etcd
  aidocumines_milvus
  aidocumines_elasticsearch
  aidocumines_ollama
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

# Keep Docker's failure reason in the journal without flooding it with unbounded output.
# pipefail preserves the Docker command's exit status through the diagnostic tail.
recover_container() {
  local action="$1" name="$2" diagnostic code
  shift 2
  if diagnostic="$(docker "$@" 2>&1 | tail -n 8 | tail -c 2000)"; then
    return 0
  else
    code=$?
    log "could not $action $name (exit $code): ${diagnostic:-no diagnostic output}"
    return "$code"
  fi
}

# An offline metadata backup/recovery must not race an automatic Milvus restart.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! source "$SCRIPT_DIR/recovery-lock.sh"; then
  log "could not load the recovery lock; skipping container recovery"
  exit 1
fi
if acquire_recovery_lock try; then
  :
else
  lock_code=$?
  if [ "$lock_code" -eq 75 ]; then
    log "another recovery operation holds the lock; skipping this run"
    exit 0
  fi
  log "could not acquire the recovery lock (exit $lock_code); skipping container recovery"
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  log "Docker is not responding"
  exit 1
fi

# A failed Compose handoff can leave external etcd beside the stopped old embedded
# container. Do not activate either possible metadata writer after the lock is released.
metadata_handoff_incomplete=0
if docker inspect --format '{{.Id}}' aidocumines_milvus >/dev/null 2>&1 \
  && docker inspect --format '{{.Id}}' aidocumines_milvus_etcd >/dev/null 2>&1; then
  metadata_mode="$(docker inspect --format '{{range .Config.Env}}{{if eq (index (split . "=") 0) "ETCD_USE_EMBED"}}{{println .}}{{end}}{{end}}' aidocumines_milvus 2>/dev/null)"
  if [ "$metadata_mode" != 'ETCD_USE_EMBED=false' ]; then
    metadata_handoff_incomplete=1
    log "Milvus metadata handoff incomplete; resume deployment/manual review. Leaving Milvus and external etcd unchanged."
  fi
fi

for name in "${CONTAINERS[@]}"; do
  if [ "$metadata_handoff_incomplete" -eq 1 ]; then
    case "$name" in aidocumines_milvus|aidocumines_milvus_etcd) continue;; esac
  fi
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
          recover_container restart "$name" restart --time 30 "$name" && rm -f "$counter"
        fi
      else
        rm -f "$counter"
      fi
      ;;
    exited|created|dead)
      oom="$(docker inspect --format '{{.State.OOMKilled}}' "$name" 2>/dev/null)"
      code="$(docker inspect --format '{{.State.ExitCode}}' "$name" 2>/dev/null)"
      log "$name is $status (exit code $code, OOM-killed: $oom) - starting"
      recover_container start "$name" start "$name" || true
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
