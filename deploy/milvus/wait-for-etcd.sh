#!/bin/sh
# Gate every Milvus start, including Docker-daemon restarts that bypass Compose
# dependency ordering. Never fall back to the embedded etcd startup race.
set -eu

if [ "${ETCD_USE_EMBED:-}" != "false" ] || [ -z "${ETCD_ENDPOINTS:-}" ]; then
  echo "Milvus requires external etcd: ETCD_USE_EMBED=false and ETCD_ENDPOINTS must be set." >&2
  exit 2
fi

wait_timeout="${MILVUS_ETCD_WAIT_TIMEOUT:-180}"
wait_interval="${MILVUS_ETCD_WAIT_INTERVAL:-2}"
case "$wait_timeout:$wait_interval" in
  *[!0-9:]*|:*|*:) echo "Milvus etcd wait timeout and interval must be positive integers." >&2; exit 2 ;;
esac
if [ "$wait_timeout" -le 0 ] || [ "$wait_interval" -le 0 ]; then
  echo "Milvus etcd wait timeout and interval must be positive integers." >&2
  exit 2
fi
if [ "$#" -eq 0 ]; then
  echo "No Milvus command was supplied." >&2
  exit 2
fi

deadline=$(( $(date +%s) + wait_timeout ))
echo "Waiting for external Milvus metadata readiness..."
while :; do
  ready=false
  # ETCD_ENDPOINTS supports a comma-separated list. One healthy member can
  # serve the single-node metadata store and proves its leader is ready.
  old_ifs=$IFS
  IFS=,
  for endpoint in $ETCD_ENDPOINTS; do
    case "$endpoint" in
      http://*|https://*) url=$endpoint ;;
      *) url="http://$endpoint" ;;
    esac
    if curl --fail --silent --noproxy '*' --connect-timeout 2 --max-time 5 "${url%/}/health" >/dev/null 2>&1; then
      ready=true
      break
    fi
  done
  IFS=$old_ifs
  if [ "$ready" = true ]; then
    echo "External Milvus metadata is ready; starting Milvus."
    exec "$@"
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "External Milvus metadata did not become ready within ${wait_timeout}s; refusing to start Milvus." >&2
    exit 1
  fi
  sleep "$wait_interval"
done
