"""
Reconcile upload pipeline state for existing files.

Fixes two production issues:
1. Files stuck in "Pending" status forever (nothing transitioned them).
2. Files with NULL document_type ("Unknown Type" on the dashboards)
   because classification used to run only inside the indexing task,
   which is skipped for non-PDF/DOCX files, disabled auto-indexing and
   duplicate-MD5 uploads.

Usage:
    python manage.py reconcile_uploads --dry-run
    python manage.py reconcile_uploads --stale-hours 1
    python manage.py reconcile_uploads --classify-missing --limit 500
    python manage.py reconcile_uploads --file-id 123
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import File
from core.tasks import extract_document_text_task
from document_search.tasks import classify_file


class Command(BaseCommand):
    help = "Repair stuck Pending statuses and missing document types on uploaded files."

    def add_arguments(self, parser):
        parser.add_argument("--stale-hours", type=float, default=1.0,
                            help="Files still Pending/Processing older than this are reconciled (default: 1).")
        parser.add_argument("--classify-missing", action="store_true",
                            help="Queue classify_file for every file with NULL/empty document_type.")
        parser.add_argument("--limit", type=int, default=None,
                            help="Max number of files to process per pass.")
        parser.add_argument("--file-id", type=int, default=None,
                            help="Reconcile a single file id (status + classification).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would happen without changing anything.")
        parser.add_argument("--refresh-insights", action="store_true",
                            help="Delete cached UserInsights snapshots so dashboards recompute on next load.")
        parser.add_argument("--retry-failed", action="store_true",
                            help="Requeue extraction+classification for files marked Failed.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]
        file_id = options["file_id"]

        completed = requeued = failed = classified = 0

        qs = File.objects.all().order_by("id")
        if file_id:
            qs = File.objects.filter(id=file_id)

        # ── 1. Stuck Pending / Processing files ──────────────────────────
        stale_cutoff = timezone.now() - timedelta(hours=options["stale_hours"])
        stuck_qs = qs.filter(
            status__in=["Pending", "Processing"], created_at__lt=stale_cutoff
        )
        if limit:
            stuck_qs = stuck_qs[:limit]

        for f in stuck_qs.iterator(chunk_size=200):
            content = f.content or ""
            # Old extractor stored literal error strings as content — treat
            # those as missing so the file gets reprocessed.
            has_content = bool(content.strip()) and not content.startswith("Error extracting text:")
            has_chunks = f.vector_chunks.exists()

            if has_content or has_chunks:
                self.stdout.write(f"[complete] file {f.id} '{f.filename}' → Completed")
                if not dry_run:
                    File.objects.filter(id=f.id).update(status="Completed")
                completed += 1
            else:
                # Never processed: requeue extraction + classification so the
                # normal lifecycle can finish it this time.
                self.stdout.write(f"[requeue]  file {f.id} '{f.filename}' → re-queued for processing")
                if not dry_run:
                    transaction_on_commit_requeue(f.id)
                requeued += 1

        # Files already Failed: leave them, but report the count.
        failed = qs.filter(status="Failed").count() if not file_id else (
            1 if qs.filter(status="Failed").exists() else 0
        )

        # ── 2. Missing or failed document_type ───────────────────────────
        if options["classify_missing"]:
            # NULL/empty = never classified (indexing was skipped);
            # "Unknown" = classification ran but no text was extractable.
            unclassified_qs = (
                qs.filter(document_type__isnull=True)
                | qs.filter(document_type="")
                | qs.filter(document_type="Unknown")
            )
            unclassified_qs = unclassified_qs.order_by("id")
            if limit:
                unclassified_qs = unclassified_qs[:limit]
            for f in unclassified_qs.iterator(chunk_size=200):
                self.stdout.write(f"[classify] file {f.id} '{f.filename}' ({f.document_type}) → queued classify_file")
                if not dry_run:
                    classify_file.delay(f.id, force=True)
                classified += 1

        summary = (
            f"\nDone. completed={completed} requeued={requeued} "
            f"classify_queued={classified} failed_total={failed}"
            f"{' (dry-run)' if dry_run else ''}"
        )
        self.stdout.write(self.style.SUCCESS(summary))

        # ── 2b. Retry Failed files with the hardened extractor ───────────
        if options["retry_failed"]:
            failed_qs = qs.filter(status="Failed").order_by("id")
            if limit:
                failed_qs = failed_qs[:limit]
            for f in failed_qs.iterator(chunk_size=200):
                self.stdout.write(f"[retry]    file {f.id} '{f.filename}' → re-queued after Failed")
                if not dry_run:
                    transaction_on_commit_requeue(f.id)
                requeued += 1

        # ── 3. Bust dashboard cache so the UI reflects reality ───────────
        if options["refresh_insights"] and not dry_run:
            from platform_data_insights.models import UserInsights
            deleted, _ = UserInsights.objects.all().delete()
            self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} cached UserInsights snapshot(s); dashboards will recompute on next load."))


def transaction_on_commit_requeue(file_id: int):
    """Requeue processing tasks immediately (command runs outside request cycle)."""
    extract_document_text_task.delay(file_id)
    classify_file.delay(file_id)
