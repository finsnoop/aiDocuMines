#!/usr/bin/env bash
# Offline, verified backup before replacing embedded etcd with the same-version
# external service. Never resets metadata or restores data automatically.
set -euo pipefail
umask 077
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
source "$SCRIPT_DIR/recovery-lock.sh"
die() { echo "Milvus migration stopped: $*" >&2; exit 1; }
for utility in docker tar gzip sha256sum mktemp df awk realpath; do
  command -v "$utility" >/dev/null 2>&1 || die "$utility is required; nothing was migrated."
done
acquire_recovery_lock wait || die "could not acquire the shared deployment/watchdog lock."
docker info --format '{{.ID}}' >/dev/null || die "Docker is unavailable."

container_exists() {
  local found
  found=$(docker container ls --all --filter "name=^/$1$" --format '{{.Names}}') || die "cannot list Docker containers."
  [[ "$found" == "$1" ]]
}
inspect() { docker inspect --format "$1" "$2"; }
assert_no_nested_mounts() {
  local destinations destination
  destinations=$(inspect '{{range .Mounts}}{{println .Destination}}{{end}}' "$1") || die "cannot inspect all storage mounts for $1."
  while IFS= read -r destination; do
    case "$destination" in
      /var/lib/milvus/*) die "$1 has a nested storage mount at $destination; a parent-volume backup would omit that data. Manual migration is required.";;
    esac
  done <<< "$destinations"
}
storage_mount() {
  assert_no_nested_mounts "$1"
  inspect '{{range .Mounts}}{{if eq .Destination "/var/lib/milvus"}}{{printf "%s\t%s\n" .Type .Name}}{{end}}{{end}}' "$1"
}
read_setting() {
  # Only these non-secret metadata settings are selected by callers.
  inspect "{{range .Config.Env}}{{if eq (index (split . \"=\") 0) \"$1\"}}{{println .}}{{end}}{{end}}" "$2"
}
validate_metadata() {
  local expected="$1" actual metadata_image metadata_path
  container_exists "$metadata" || die "the expected external etcd container is missing; refusing to switch metadata stores."
  actual=$(storage_mount "$metadata") || die "cannot inspect external etcd storage."
  [[ "$actual" == $'volume\t'"$expected" ]] || die "external etcd uses a different metadata volume; manual resolution is required."
  metadata_image=$(inspect '{{.Config.Image}}' "$metadata") || die "cannot inspect external etcd version."
  [[ "$metadata_image" == quay.io/coreos/etcd:v3.5.23 ]] || die "external etcd must use the expected 3.5.23 image."
  metadata_path=$(inspect '{{range .Config.Cmd}}{{if eq . "--data-dir=/var/lib/milvus/etcd"}}{{println .}}{{end}}{{end}}' "$metadata") || die "cannot inspect external etcd data path."
  [[ "$metadata_path" == '--data-dir=/var/lib/milvus/etcd' ]] || die "external etcd data path is unverified; manual resolution is required."
}
old=aidocumines_milvus
metadata=aidocumines_milvus_etcd
project="${AIDOCUMINES_COMPOSE_PROJECT:-}"
[[ -z "$project" || "$project" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "invalid Compose project identity."
if ! container_exists "$old"; then
  [[ -n "$project" ]] || die "no Milvus container or Compose project identity; run restart_containers.sh to resolve storage safely."
  expected_volume="${project}_milvus-data"
  existing=$(docker volume ls --quiet --filter "name=^${expected_volume}$") || die "cannot check pre-existing Milvus storage."
  if [[ -n "$existing" ]]; then
    [[ "$existing" == "$expected_volume" ]] || die "ambiguous Milvus volume without its original container."
    validate_metadata "$expected_volume"
    echo "Existing external etcd retains the project volume; Compose can recreate the missing Milvus container."
    exit 0
  fi
  if container_exists "$metadata"; then
    die "external etcd exists without the expected project volume; refusing to initialize a different store."
  fi
  echo "No existing Milvus container; Compose can initialize its external metadata service."
  exit 0
fi
# Inspect only this non-secret flag, never dump the container environment.
embed=$(read_setting ETCD_USE_EMBED "$old") || die "cannot inspect Milvus metadata mode."
if [[ "$embed" != 'ETCD_USE_EMBED=false' ]] && container_exists "$metadata"; then
  assert_no_nested_mounts "$metadata"
  metadata_state=$(inspect '{{.State.Status}}' "$metadata") || die "cannot inspect external etcd."
  case "$metadata_state" in
    running|restarting|paused) die "embedded Milvus and external etcd coexist. Stop the conflicting writers deliberately before retrying; neither was changed.";;
  esac
fi
mount=$(storage_mount "$old") || die "cannot inspect Milvus storage."
[[ "$mount" != *$'\n'* ]] || die "multiple Milvus storage mounts are ambiguous."
IFS=$'\t' read -r mount_type volume <<< "$mount"
[[ "$mount_type" == volume && "$volume" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || die "expected an existing named volume at /var/lib/milvus; bind mounts or missing mounts require manual migration."
if [[ -n "$project" && "$volume" != "${project}_milvus-data" ]]; then
  die "the actual Milvus volume does not match the selected Compose project; refusing to attach an empty replacement."
fi
volume_description() {
  docker volume inspect --format '{{.Name}}|{{.CreatedAt}}|{{.Driver}}|{{.Mountpoint}}' "$volume"
}
description=$(volume_description) || die "the mounted Milvus volume does not exist."
[[ -n "$description" ]] || die "Milvus volume identity is empty."
if [[ "$embed" == 'ETCD_USE_EMBED=false' ]]; then
  endpoints=$(read_setting ETCD_ENDPOINTS "$old") || die "cannot inspect external metadata endpoints."
  [[ "$endpoints" == 'ETCD_ENDPOINTS=milvus_etcd:2379' ]] || die "Milvus uses an unknown external metadata endpoint; no settings were changed."
  validate_metadata "$volume"
  echo "Milvus already uses the verified external etcd volume; no migration is needed."
  exit 0
fi
data_dir=$(read_setting ETCD_DATA_DIR "$old") || die "cannot inspect embedded etcd data path."
[[ "$data_dir" == 'ETCD_DATA_DIR=/var/lib/milvus/etcd' ]] || die "embedded etcd data path is missing or custom; manual resolution is required."
identity=$(printf '%s' "$description" | sha256sum | awk '{print $1}')
mountpoint=${description##*|}
image=$(inspect '{{.Image}}' "$old") || die "cannot resolve the installed Milvus image."
container_id=$(inspect '{{.Id}}' "$old") || die "cannot resolve the existing Milvus container."
[[ "$image" =~ ^sha256:[0-9a-f]{64}$ && "$container_id" =~ ^[0-9a-f]{64}$ ]] || die "invalid Docker image/container identity."
docker image inspect --format '{{.Id}}' "$image" >/dev/null || die "the original Milvus image is not installed; no backup container will be pulled."
backup_root=$(realpath -m -- "${MILVUS_BACKUP_DIR:-$REPO_DIR/backups/milvus}") || die "cannot resolve the backup directory."
[[ "$backup_root" != / ]] || die "use a dedicated backup directory."
if [[ -n "$mountpoint" && ( "$backup_root" == "$mountpoint" || "$backup_root" == "$mountpoint/"* ) ]]; then
  die "backup directory must be outside the source volume."
fi
backup_dir="$backup_root/$identity"
[[ ! -L "$backup_dir" ]] || die "backup directory must not be a symlink."
mkdir -p -- "$backup_dir" || die "cannot create the backup directory."
chmod 700 -- "$backup_dir" || die "cannot protect the backup directory."
partial=''
receipt_tmp=''
cleanup() {
  [[ -z "$partial" || ! -f "$partial" ]] || rm -f -- "$partial"
  [[ -z "$receipt_tmp" || ! -f "$receipt_tmp" ]] || rm -f -- "$receipt_tmp"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

state=$(inspect '{{.State.Status}}' "$old") || die "cannot inspect Milvus state."
case "$state" in
  paused|removing) die "Milvus is $state; resolve that state deliberately before migration.";;
  running|restarting)
    echo "Stopping embedded Milvus for an offline backup of volume $volume."
    docker stop --time 60 "$old" >/dev/null || die "Milvus could not be stopped; no backup was attempted."
    ;;
esac
assert_offline() {
  local users actual actual_container
  actual=$(volume_description) || die "cannot recheck the source volume."
  [[ "$actual" == "$description" ]] || die "source volume identity changed; refusing migration."
  actual_container=$(inspect '{{.Id}}' "$old") || die "the original Milvus container disappeared."
  [[ "$actual_container" == "$container_id" ]] || die "Milvus container changed during migration."
  users=$(docker ps --filter "volume=$volume" --format '{{.Names}}') || die "cannot check source-volume writers."
  [[ -z "$users" ]] || die "a container still has the source volume running: $users. Milvus remains stopped."
}
assert_offline
read_volume() {
  docker run --rm --network none --read-only --security-opt no-new-privileges \
    --mount "type=volume,source=$volume,target=/source,readonly" \
    --env LC_ALL=C --entrypoint "$1" "$image" "${@:2}"
}
volume_tar() {
  read_volume tar --format=gnu --sort=name --numeric-owner --sparse --atime-preserve=system -cf - -C /source .
}
snapshot_hash() { volume_tar | sha256sum | awk '{print $1}'; }
valid_archive() {
  gzip -t -- "$1" && tar -tzf "$1" >/dev/null
}
receipt="$backup_dir/verified-snapshot"
# A receipt is never sufficient by itself: recheck archive integrity AND all
# current source bytes/metadata before reusing a prior backup after a failed up.
if [[ -f "$receipt" && ! -L "$receipt" ]]; then
  mapfile -t saved < "$receipt"
  if [[ "${saved[0]:-}" == 'version=1' && "${saved[1]:-}" == "volume=$volume" && "${saved[2]:-}" == "identity=$identity" ]]; then
    previous=${saved[3]:-}; previous=${previous#archive=}
    saved_hash=${saved[4]:-}; saved_hash=${saved_hash#snapshot_sha256=}
    archive_hash=${saved[5]:-}; archive_hash=${archive_hash#archive_sha256=}
    if [[ "$previous" =~ ^[a-zA-Z0-9_.-]+\.tar\.gz$ && "$saved_hash" =~ ^[0-9a-f]{64}$ && "$archive_hash" =~ ^[0-9a-f]{64}$ && -f "$backup_dir/$previous" && ! -L "$backup_dir/$previous" ]]; then
      if valid_archive "$backup_dir/$previous" && [[ "$(sha256sum -- "$backup_dir/$previous" | awk '{print $1}')" == "$archive_hash" && "$(gzip -cd -- "$backup_dir/$previous" | sha256sum | awk '{print $1}')" == "$saved_hash" ]]; then
        current_hash=$(snapshot_hash) || die "could not verify the current offline snapshot; Milvus remains stopped."
        assert_offline
        if [[ "$current_hash" == "$saved_hash" ]]; then
          echo "Verified existing offline backup: $backup_dir/$previous"
          exit 0
        fi
      fi
    fi
  fi
fi
used_kb=$(read_volume du -sk /source | awk '{print $1}') || die "cannot estimate backup size; Milvus remains stopped."
free_kb=$(df -Pk -- "$backup_dir" | awk 'NR == 2 {print $4}')
[[ "$used_kb" =~ ^[0-9]+$ && "$free_kb" =~ ^[0-9]+$ ]] || die "cannot check backup disk capacity; Milvus remains stopped."
required_kb=$((used_kb + used_kb / 10 + 65536))
[[ "$free_kb" -ge "$required_kb" ]] || die "insufficient backup disk space (need at least ${required_kb} KiB, available ${free_kb} KiB); Milvus remains stopped."
partial=$(mktemp "$backup_dir/$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX.tar.gz.partial") || die "cannot create a private backup file."
if ! volume_tar | gzip -c > "$partial"; then
  die "offline backup failed; deployment must stop. Original data was not changed and Milvus remains stopped."
fi
valid_archive "$partial" || die "backup integrity check failed; deployment must stop."
snapshot=$(gzip -cd -- "$partial" | sha256sum | awk '{print $1}') || die "cannot fingerprint the backup."
current_hash=$(snapshot_hash) || die "cannot verify the source against its backup."
assert_offline
[[ "$current_hash" == "$snapshot" ]] || die "source contents changed during backup; deployment must stop."
archive=${partial%.partial}
[[ ! -e "$archive" ]] || die "backup archive name already exists; refusing to overwrite it."
mv -- "$partial" "$archive"
partial=''
archive_hash=$(sha256sum -- "$archive" | awk '{print $1}')
receipt_tmp=$(mktemp "$backup_dir/receipt-XXXXXX.tmp")
printf 'version=1\nvolume=%s\nidentity=%s\narchive=%s\nsnapshot_sha256=%s\narchive_sha256=%s\ncontainer_id=%s\nimage_id=%s\n' \
  "$volume" "$identity" "${archive##*/}" "$snapshot" "$archive_hash" "$container_id" "$image" > "$receipt_tmp"
mv -f -- "$receipt_tmp" "$receipt"
receipt_tmp=''
echo "Verified offline Milvus backup: $archive"
echo "Original data remains in $volume. Continue with restart_containers.sh while holding the shared recovery lock; no automatic restore is performed."
