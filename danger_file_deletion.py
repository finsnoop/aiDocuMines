#!/usr/bin/env python
"""
DANGER - DEVELOPMENT AND TESTING ONLY.

Wipes every document in the platform, for every organisation, in one go:
  * the files on disk: the document folders under MEDIA_ROOT (on the server
    that is <API checkout>/media, mounted in the containers at /app/media)
  * the database rows: files, versions, folder links, OCR / translation /
    anonymisation / structure / redlining / workflow outputs, analytics
    snapshots, connector jobs, and (unless --keep-folders) the sidebar folders
  * the document cache in Redis, the Elasticsearch index and the Milvus vectors
  * optionally the sign-in service's document caches (--node-db-url)

Kept: user accounts, organisations, roles, settings, matters / projects,
workflow and template definitions, integration configurations.

There is no undo. Take a database backup first if anything matters.

Run on the server (stop the workers first so nothing is written while it runs):
    docker stop aidocumines_celery aidocumines_celery_fast aidocumines_celery_beat aidocumines_file_monitor
    docker exec -it aidocumines_web python danger_file_deletion.py --dry-run
    docker exec -it aidocumines_web python danger_file_deletion.py
    docker start aidocumines_celery aidocumines_celery_fast aidocumines_celery_beat aidocumines_file_monitor

Options:
    --dry-run                 show what would be deleted, delete nothing
    --confirm "DELETE ALL DOCUMENTS"   skip the typed prompt (for scripts)
    --keep-folders            keep the (now empty) sidebar folder tree
    --everything-in-media     delete every entry under MEDIA_ROOT, not only document folders
    --node-db-url URL         also empty the sign-in service's document cache tables,
                              e.g. postgresql://user:pass@host:5432/dbname (or set NODE_DB_URL)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

CONFIRM_PHRASE = "DELETE ALL DOCUMENTS"

# Media folders that hold documents or files derived from them, besides the ones
# core.document_purge.DOCUMENT_DIRS already lists.
EXTRA_DOCUMENT_DIRS = (
    "demo", "bulk", "previews", "scale", "local-audit", "service_outputs", "pe_lp_reports", "ocr_cache",
)

# The sign-in service's per-document cache tables (never its users or sessions).
NODE_CACHE_TABLES = ("files", "ocr", "anonymization", "translation", "activitylogs")


def setup_django():
    from django.apps import apps
    if apps.ready:
        return
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aiDocuMines.settings")
    import django
    django.setup()


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def tree_size(path: Path) -> tuple[int, int]:
    files = size = 0
    for current, _dirs, names in os.walk(path, followlinks=False):
        for name in names:
            p = Path(current) / name
            try:
                if not p.is_symlink():
                    size += p.stat().st_size
                    files += 1
            except OSError:
                pass
    return files, size


def media_targets(media: Path, everything: bool) -> list[Path]:
    from core.document_purge import DOCUMENT_DIRS
    if not media.is_dir():
        return []
    if everything:
        return sorted(media.iterdir())
    names = dict.fromkeys((*DOCUMENT_DIRS, *EXTRA_DOCUMENT_DIRS))
    return [media / name for name in names if (media / name).exists() or (media / name).is_symlink()]


def remove_entry(path: Path):
    """Delete one top-level media entry without ever following a symlink out of media."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def node_tables(url: str) -> dict[str, int]:
    import psycopg2
    with psycopg2.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name = ANY(%s)",
                    (list(NODE_CACHE_TABLES),))
        found = sorted(r[0] for r in cur.fetchall())
        counts = {}
        for table in found:  # names come from the fixed allowlist above
            cur.execute(f'SELECT count(*) FROM "{table}"')
            counts[table] = cur.fetchone()[0]
        return counts


def truncate_node_tables(url: str, tables) -> None:
    import psycopg2
    if not tables:
        return
    with psycopg2.connect(url) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in tables if t in NODE_CACHE_TABLES) + " RESTART IDENTITY CASCADE")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="DANGER: delete every document from disk and database (dev/test only).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--keep-folders", action="store_true")
    parser.add_argument("--everything-in-media", action="store_true")
    parser.add_argument("--node-db-url", default=os.getenv("NODE_DB_URL", ""))
    args = parser.parse_args(argv)

    setup_django()
    from django.conf import settings
    from django.db import transaction
    from core import document_purge as purge

    media = Path(settings.MEDIA_ROOT).absolute()
    roots = purge.owned_roots()          # refuses "/", the code folder, home, or a symlinked media root
    folder_model = purge.model("document_operations.Folder")

    # ---- inventory -----------------------------------------------------------------------
    targets = media_targets(media, args.everything_in_media)
    disk = {}
    for t in targets:
        disk[t.name] = {"symlink": True} if t.is_symlink() else dict(zip(("files", "bytes"), tree_size(t) if t.is_dir() else (1, t.stat().st_size)))
    recorded, _missing, path_issues = purge.inventory_paths(roots)
    covered = [t.resolve() for t in targets if not t.is_symlink()]
    stray = sorted(p for p in recorded if not any(p.is_relative_to(c) for c in covered))
    rows = {label: q.count() for label, q in purge.deletion_queries()}
    rows = {k: v for k, v in rows.items() if v}
    if folder_model is not None and not args.keep_folders:
        rows["document_operations.Folder"] = folder_model.objects.count()
    try:
        cache_keys = purge.document_cache_keys()
    except Exception as exc:  # e.g. a cache backend that cannot list keys
        cache_keys, cache_note = set(), f"skipped ({exc})"
    else:
        cache_note = f"{len(cache_keys)} keys"
    node = {}
    if args.node_db_url:
        try:
            node = node_tables(args.node_db_url)
        except Exception as exc:
            print(f"! Cannot reach the sign-in service database: {type(exc).__name__}: {exc}")
            return 2

    total_files = sum(v.get("files", 0) for v in disk.values()) + len(stray)
    total_bytes = sum(v.get("bytes", 0) for v in disk.values())
    print("=" * 72)
    print(" DANGER: DELETE ALL DOCUMENTS  (development / testing only)")
    print("=" * 72)
    print(f"Media folder : {media}")
    for name, info in disk.items():
        print(f"  rm -rf {name:<22} " + ("(symlink: link removed, target untouched)" if info.get("symlink")
                                         else f"{info['files']} files, {human(info['bytes'])}"))
    if stray:
        print(f"  + {len(stray)} recorded files elsewhere inside media")
    if path_issues:
        print(f"  ! {len(path_issues)} recorded paths are outside media or unsafe and will NOT be touched on disk:")
        for issue in path_issues[:10]:
            print(f"      {issue}")
    print(f"Database rows: {sum(rows.values())}")
    for label, n in sorted(rows.items()):
        print(f"  {label:<48} {n}")
    print(f"Redis cache  : {cache_note}")
    print("Search       : Elasticsearch document index and Milvus document vectors emptied")
    print("Sign-in svc  : " + (", ".join(f"{t}={n}" for t, n in node.items()) or "no cache tables found")
          if args.node_db_url else "Sign-in svc  : not touched (pass --node-db-url to include)")
    print(f"Total on disk: {total_files} files, {human(total_bytes)}")
    print("-" * 72)

    if args.dry_run:
        print("Dry run: nothing was deleted.")
        return 0
    if args.confirm != CONFIRM_PHRASE:
        if not sys.stdin.isatty():
            print(f'Refusing: not interactive. Pass --confirm "{CONFIRM_PHRASE}" to run unattended.')
            return 1
        typed = input(f'Type "{CONFIRM_PHRASE}" to delete everything listed above: ').strip()
        if typed != CONFIRM_PHRASE:
            print("Cancelled. Nothing was deleted.")
            return 1

    problems = []

    # ---- database ------------------------------------------------------------------------
    with transaction.atomic():
        deleted = purge.delete_document_rows()
        if folder_model is not None and not args.keep_folders:
            # Children first is handled by the ORM cascade; links/permissions on folders go with them.
            _, counts = folder_model.objects.all().delete()
            for k, v in counts.items():
                deleted[k] = deleted.get(k, 0) + v
    print(f"Database: deleted {sum(deleted.values())} rows")

    # ---- disk ----------------------------------------------------------------------------
    removed = 0
    for t in targets:
        try:
            remove_entry(t)
            removed += 1
        except OSError as exc:
            problems.append(f"Could not remove {t}: {exc}")
    for p in stray:
        try:
            purge.unlink_document(p, roots)
        except Exception as exc:
            problems.append(f"Could not remove {p}: {exc}")
    (media / "uploads").mkdir(parents=True, exist_ok=True)   # the upload code expects it
    print(f"Disk: removed {removed} media entries and {len(stray)} other recorded files")

    # ---- cache, search, sign-in service ---------------------------------------------------
    try:
        purge.clear_document_cache(cache_keys)
    except Exception as exc:
        problems.append(f"Cache: {type(exc).__name__}: {exc}")
    search, errors = purge.search_inventory(execute=True)
    problems.extend(errors)
    print(f"Search: {json.dumps(search)}")
    if node:
        try:
            truncate_node_tables(args.node_db_url, list(node))
            print(f"Sign-in service: emptied {', '.join(node)}")
        except Exception as exc:
            problems.append(f"Sign-in service: {type(exc).__name__}: {exc}")

    remaining = purge.model("core.File").objects.count()
    if remaining:
        problems.append(f"{remaining} document rows appeared while running: a worker or upload is still active.")
    print("-" * 72)
    if problems:
        print("Finished with warnings:")
        for p in problems:
            print(f"  ! {p}")
        return 3
    print("Done. Every document is gone; accounts and settings are untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
