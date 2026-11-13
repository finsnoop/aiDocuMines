"""
Database backups of the main platform database (PostgreSQL custom-format dumps).

Stored in BACKUP_DIR (default: <project>/backups, never under MEDIA_ROOT so they
are not web-served). Each backup "<name>.dump" has a "<name>.json" sidecar with
its status. Document files on disk are not included: back up the media volume
with your storage snapshots.

Restore replaces the live database and is off unless ENABLE_BACKUP_RESTORE=true.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone

from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}\.dump$")


def backup_dir() -> str:
    d = os.getenv("BACKUP_DIR") or os.path.join(settings.BASE_DIR, "backups")
    os.makedirs(d, exist_ok=True)
    return d


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or "")) and ".." not in name


def _paths(name):
    d = backup_dir()
    return os.path.join(d, name), os.path.join(d, name[:-5] + ".json")


def read_meta(name) -> dict:
    dump, meta = _paths(name)
    data = {}
    if os.path.exists(meta):
        try:
            with open(meta, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
    if os.path.exists(dump):
        data["size"] = os.path.getsize(dump)
    data["name"] = name
    return data


def write_meta(name, **fields):
    _, meta = _paths(name)
    data = read_meta(name) if os.path.exists(meta) else {"name": name}
    data.update(fields)
    with open(meta, "w", encoding="utf-8") as fh:
        json.dump(data, fh, default=str)


def list_backups() -> list:
    d = backup_dir()
    names = {f for f in os.listdir(d) if f.endswith(".dump")} | {f[:-5] + ".dump" for f in os.listdir(d) if f.endswith(".json")}
    items = [read_meta(n) for n in names if valid_name(n)]
    return sorted(items, key=lambda m: str(m.get("started_at", "")), reverse=True)


def _pg_env_args():
    db = settings.DATABASES["default"]
    env = {**os.environ, "PGPASSWORD": str(db.get("PASSWORD") or "")}
    args = ["-h", str(db.get("HOST") or "localhost"), "-p", str(db.get("PORT") or "5432"),
            "-U", str(db.get("USER") or "postgres")]
    return env, args, str(db.get("NAME"))


def new_backup_name() -> str:
    return datetime.now(timezone.utc).strftime("aidocumines-%Y%m%d-%H%M%S.dump")


@shared_task(name="core.create_database_backup", acks_late=True)
def create_database_backup(name: str, requested_by: str = "") -> dict:
    dump, _ = _paths(name)
    write_meta(name, status="running", started_at=datetime.now(timezone.utc).isoformat(), requested_by=requested_by)
    env, args, dbname = _pg_env_args()
    try:
        proc = subprocess.run(["pg_dump", "-Fc", "--no-owner", *args, "-d", dbname, "-f", dump],
                              env=env, capture_output=True, text=True, timeout=6 * 3600)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[-800:] or f"pg_dump exited with {proc.returncode}")
        write_meta(name, status="completed", finished_at=datetime.now(timezone.utc).isoformat())
        return {"status": "completed"}
    except Exception as exc:
        logger.exception("Backup %s failed", name)
        write_meta(name, status="failed", error=str(exc)[:800], finished_at=datetime.now(timezone.utc).isoformat())
        if os.path.exists(dump):
            os.remove(dump)
        return {"status": "failed"}


@shared_task(name="core.restore_database_backup", acks_late=True)
def restore_database_backup(name: str, requested_by: str = "") -> dict:
    dump, _ = _paths(name)
    write_meta(name, restore_status="running", restore_started_at=datetime.now(timezone.utc).isoformat(),
               restore_requested_by=requested_by)
    env, args, dbname = _pg_env_args()
    try:
        proc = subprocess.run(["pg_restore", "--clean", "--if-exists", "--no-owner", *args, "-d", dbname, dump],
                              env=env, capture_output=True, text=True, timeout=6 * 3600)
        if proc.returncode not in (0, 1):    # 1 = warnings (e.g. objects that did not exist)
            raise RuntimeError(proc.stderr.strip()[-800:] or f"pg_restore exited with {proc.returncode}")
        write_meta(name, restore_status="completed", restore_finished_at=datetime.now(timezone.utc).isoformat())
        return {"status": "completed"}
    except Exception as exc:
        logger.exception("Restore of %s failed", name)
        write_meta(name, restore_status="failed", restore_error=str(exc)[:800])
        return {"status": "failed"}
