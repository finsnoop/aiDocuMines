"""Copy the Playbooks page's library from the Node sign-in service's database into the platform (contracts.Playbook).

    python manage.py import_node_playbooks --node-db postgresql://user:pass@host:5432/dbname [--files-dir DIR] [--apply]

Without --apply nothing is written: every row is listed with the organisation it maps to, or why it is skipped.
Safe to run again: rows already copied (same Node id, kept in Playbook.legacy_id) are updated, not duplicated.

Organisation mapping: the Node row's client_id is the creating person's OAuth application id (or empty); that
application's user gives the organisation. Failing that, the creator's email (created_by). Rows that map to no
organisation are skipped and listed: they were visible to every organisation before the 2026-11-21 fix and must
not be assigned to one by guesswork.
Uploaded files: the Node container stored them under PLAYBOOKS_DIR. Pass --files-dir pointing at a copy of that
folder; each file is stored through the shared ingestion pipeline (core/ingest.py) as the creator, in the
organisation's "Knowledge library" matter. Missing files are reported; the playbook is still copied.
"""
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

CATEGORY_IDS = {"legal", "finance", "hr", "operations", "compliance"}


class Command(BaseCommand):
    help = "Copy Node playbooks into the platform (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--node-db", default=os.getenv("NODE_DATABASE_URL", ""),
                            help="Node service database URL (or NODE_DATABASE_URL).")
        parser.add_argument("--files-dir", default="", help="Copy of the Node PLAYBOOKS_DIR, for uploaded files.")
        parser.add_argument("--apply", action="store_true", help="Write the playbooks (default: dry run).")

    def handle(self, *args, node_db, files_dir, apply, **opts):
        import psycopg2
        import psycopg2.extras
        if not node_db:
            raise CommandError("Give --node-db (or set NODE_DATABASE_URL).")
        with psycopg2.connect(node_db) as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM playbooks ORDER BY created_at")
            rows = cur.fetchall()
            try:
                cur.execute("SELECT id, name FROM playbook_tags")
                tag_names = {r["id"]: r["name"] for r in cur.fetchall()}
            except Exception:
                conn.rollback()
                tag_names = {}
        copied = updated = skipped = files_missing = 0
        for row in rows:
            user = self.owner(row)
            label = f"{row['name']!r} ({row['id']})"
            if user is None or not user.client_id:
                skipped += 1
                self.stdout.write(self.style.WARNING(f"SKIP {label}: no organisation for client_id={row.get('client_id')!r}, "
                                                     f"created_by={row.get('created_by')!r}"))
                continue
            path = self.file_path(row, files_dir)
            if row.get("file_path") and path is None:
                files_missing += 1
            if not apply:
                self.stdout.write(f"WOULD COPY {label} -> organisation {user.client_id} ({user.client.name}), owner {user.email}"
                                  + (", with file" if path else (", FILE MISSING" if row.get("file_path") else "")))
                continue
            created = self.copy(row, user, path, tag_names)
            copied += created
            updated += not created
            self.stdout.write(f"{'COPIED' if created else 'UPDATED'} {label} -> organisation {user.client_id}")
        verb = "copied" if apply else "would be copied"
        self.stdout.write(self.style.SUCCESS(
            f"{len(rows) - skipped} {verb} ({copied} new, {updated} updated); {skipped} skipped (no organisation); "
            f"{files_missing} uploaded files not found" + ("" if apply else ". Dry run: nothing written; add --apply.")))

    def owner(self, row):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        cid = str(row.get("client_id") or "").strip()
        if cid:
            try:
                from oauth2_provider.models import Application
                app = Application.objects.filter(client_id=cid).select_related("user").first()
                if app and app.user and app.user.is_active:
                    return app.user
            except Exception:
                pass
        email = str(row.get("created_by") or "").strip()
        if "@" in email:
            return User.objects.filter(email__iexact=email, is_active=True).select_related("client").first()
        if email.isdigit():
            return User.objects.filter(pk=int(email), is_active=True).select_related("client").first()
        return None

    def file_path(self, row, files_dir):
        stored = row.get("file_path")
        if not stored or not files_dir:
            return None
        candidate = os.path.join(files_dir, os.path.basename(stored))
        return candidate if os.path.isfile(candidate) else None

    @transaction.atomic
    def copy(self, row, user, path, tag_names) -> bool:
        from contracts.models import Playbook
        category = str(row.get("category_id") or "")[:60]
        defaults = {
            "name": str(row.get("name") or "Playbook")[:255], "description": row.get("description") or "",
            "content": row.get("content") or "", "category": category,
            "tags": [tag_names.get(t, t) for t in (row.get("tag_ids") or [])][:20],
            "industry": (row.get("industry") or "")[:100], "document_type": (row.get("document_type") or "")[:100],
            "use_case": (row.get("use_case") or "")[:100], "version": int(row.get("version") or 1),
            "metadata": {**(row.get("metadata") or {}), "imported_from": "node", "node_is_public": bool(row.get("is_public"))},
            "created_by": user, "updated_by": user,
        }
        pb, created = Playbook.objects.update_or_create(client_id=user.client_id, legacy_id=str(row["id"]), defaults=defaults)
        if path and pb.source_file_id is None:
            from core.ingest import MatterBatch
            from contracts.knowledge import knowledge_matter
            batch = MatterBatch(user, knowledge_matter(user), source="knowledge-import")
            try:
                with open(path, "rb") as fh:
                    entry = batch.add_stream(row.get("file_name") or os.path.basename(path), iter(lambda: fh.read(1 << 20), b""))
            finally:
                batch.finish()
            if entry.get("file_id"):
                Playbook.objects.filter(pk=pb.pk).update(source_file_id=entry["file_id"])
        return created
