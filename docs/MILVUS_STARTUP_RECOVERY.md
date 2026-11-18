# Milvus startup and reboot recovery

## Incident and cause

On 18 November 2026, production Milvus 2.6.17 repeatedly exited with code 134 while initializing its session ID: `panic: etcdserver: leader changed`. Its embedded etcd elected a leader while Milvus already had a pending metadata request. Docker and the watchdog timer were enabled and active; Docker reported `OOMKilled=false` and the host had about 12 GiB available. Those observations identify this startup failure rather than a missing Docker boot service or an observed OOM kill.

The failure matches [Milvus issue #53144](https://github.com/milvus-io/milvus/issues/53144). Its upstream readiness fix is described in [PR #53367](https://github.com/milvus-io/milvus/pull/53367). Increasing a session registration retry count or sleeping before launching embedded Milvus does not gate the metadata store created inside that same process.

## Deployment behavior

Milvus stays pinned to **2.6.17**, with its existing local storage and RocksMQ. A separate **etcd 3.5.23** process opens the existing `/var/lib/milvus/etcd` member directory in the same Docker named volume. No collection, vector index or metadata directory is initialized afresh during migration.

The metadata container has no published ports and belongs only to an internal network shared with Milvus. Its healthcheck verifies etcd readiness. Milvus additionally checks readiness in its entrypoint on every process start, so reboot recovery does not depend solely on Compose's startup ordering. The original Tini init process is retained. The metadata container's default memory cap is 512 MiB; `MILVUS_ETCD_MEM_LIMIT` in `.env` can adjust it.

`restart_containers.sh` builds and checks that the pinned metadata image is available, then holds a shared recovery lock through preparation and Compose startup. `deploy/prepare-milvus-etcd.sh` stops embedded Milvus and verifies that no container is using its volume before making a private, read-only, complete offline backup. It validates archive integrity, volume identity and the source fingerprint before allowing the handoff. A failed backup aborts deployment and leaves Milvus stopped. It never deletes or restores the source data automatically.

Backups are under `backups/milvus/` and excluded from Git and Docker build contexts. `MILVUS_BACKUP_DIR=/an/absolute/path ./restart_containers.sh` can select another host backup directory. Preserve the archive path printed by preparation. Retrying a failed handoff reuses an earlier archive only after verifying its integrity and that the current source snapshot is unchanged.

## First deployment from embedded etcd

The old watchdog does not yet participate in shared locking. Stop its timer and any in-progress run before pulling this update, so it cannot restart embedded Milvus during the offline backup:

```bash
cd ~/Apps/aiDocuMines &&
sudo systemctl stop aidocumines-watchdog.timer aidocumines-watchdog.service &&
git pull --ff-only &&
./restart_containers.sh &&
sudo systemctl start aidocumines-watchdog.timer
```

If deployment fails, leave the watchdog stopped while reviewing the reported failure and backup path. The updated watchdog also refuses to start or restart either metadata process if an old embedded configuration coexists with the new external container; this protects a partially completed handoff. Do not switch back to embedded etcd while the external metadata process is running against the same volume. A rollback needs an operator-reviewed offline restore with both metadata writers stopped.

Check the result:

```bash
docker inspect --format '{{.Name}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' aidocumines_milvus_etcd aidocumines_milvus
docker logs --tail 30 aidocumines_milvus
systemctl is-active aidocumines-watchdog.timer
```

Normal later deployments use `./restart_containers.sh`; the updated watchdog skips recovery while the shared lock is held. Application-only CI deployment does not perform this first infrastructure migration. `aidocumines_db_prepare` is an initialization job and is expected to exit successfully.

## Recovery diagnostics

The watchdog monitors the metadata store and Ollama as well as the other existing long-running containers. It records a bounded Docker error when a start/restart fails. A Compose deployment failure reports each unready long-running container and its log command instead of always pointing to PostgreSQL.

This change addresses the demonstrated Milvus startup panic and improves this application's recovery. Other applications on the VPS still require their own actual container states, restart policies and boot logs to diagnose independently.
