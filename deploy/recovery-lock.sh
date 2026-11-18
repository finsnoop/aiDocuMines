#!/usr/bin/env bash
# Shared by deployments, the migration backup and the host watchdog.
# Keep the exported descriptor open for the entire recovery/deployment handoff.
acquire_recovery_lock() {
  local mode="${1:-wait}" lock_file fd result
  case "$mode" in wait|try) ;; *) echo "Invalid recovery lock mode: $mode" >&2; return 2;; esac
  command -v flock >/dev/null 2>&1 || { echo "flock is required for safe recovery." >&2; return 1; }
  lock_file="${AIDOCUMINES_RECOVERY_LOCK_FILE:-/var/tmp/aidocumines-recovery.lock}"
  if [[ -L "$lock_file" || ( -e "$lock_file" && ! -f "$lock_file" ) ]]; then
    echo "Recovery lock must be a regular file, not a symlink: $lock_file" >&2
    return 1
  fi
  if [[ ! -e "$lock_file" ]]; then
    # Never truncate/replace an existing inode: all users must lock the same file.
    (umask 022; set -o noclobber; : > "$lock_file") 2>/dev/null || {
      [[ -f "$lock_file" && ! -L "$lock_file" ]] || { echo "Cannot create recovery lock: $lock_file" >&2; return 1; }
    }
  fi
  if [[ -n "${AIDOCUMINES_RECOVERY_LOCK_FD:-}" ]]; then
    fd="$AIDOCUMINES_RECOVERY_LOCK_FD"
    if [[ ! "$fd" =~ ^[0-9]+$ || "$fd" -lt 3 || ! "$lock_file" -ef "/proc/self/fd/$fd" ]]; then
      echo "Inherited recovery lock descriptor does not refer to $lock_file" >&2
      return 1
    fi
  else
    # Linux flock permits an exclusive lock on a read-only descriptor. A lock
    # created by the root watchdog therefore also works for the deployment user.
    exec {fd}< "$lock_file" || { echo "Cannot open recovery lock: $lock_file" >&2; return 1; }
  fi
  if [[ -L "$lock_file" || ! -f "$lock_file" || ! "$lock_file" -ef "/proc/self/fd/$fd" ]]; then
    echo "Recovery lock changed while it was being opened: $lock_file" >&2
    return 1
  fi
  if [[ "$mode" == try ]]; then
    if flock --exclusive --nonblock --conflict-exit-code 75 "$fd"; then result=0; else result=$?; fi
  else
    if flock --exclusive --timeout 120 --conflict-exit-code 75 "$fd"; then result=0; else result=$?; fi
  fi
  if [[ "$result" -ne 0 ]]; then
    [[ "$result" -eq 75 ]] || echo "Could not acquire recovery lock: $lock_file" >&2
    return "$result"
  fi
  export AIDOCUMINES_RECOVERY_LOCK_FILE="$lock_file"
  export AIDOCUMINES_RECOVERY_LOCK_FD="$fd"
}
