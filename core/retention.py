"""
Retention and disposition.

A RetentionSchedule says how long matching documents are kept. A daily review proposes documents past
retention as a DispositionBatch; nothing is destroyed until a person with retention.manage approves it.
Approval deletes through the normal safe path, so documents under legal hold or checked out are kept
(and recorded as such). Rejected documents are kept and not proposed again by that schedule.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)
MAX_BATCH = 1000


def candidates(schedule):
    """Documents of the schedule's organisation past retention, not held, not already decided or pending."""
    from core.holds import held_q
    from core.models import DispositionItem, File
    cutoff = timezone.now() - timedelta(days=schedule.retain_days)
    qs = File.objects.filter(user__client_id=schedule.client_id, created_at__lt=cutoff)
    if schedule.matter_id:
        qs = qs.filter(project_id=schedule.matter.name)
    if schedule.document_type:
        qs = qs.filter(document_type=schedule.document_type)
    if schedule.service_id:
        qs = qs.filter(service_id=schedule.service_id)
    decided = DispositionItem.objects.filter(batch__schedule=schedule).filter(
        batch__status__in=["pending", "rejected"]).values("file_ref")
    return qs.exclude(pk__in=decided).exclude(held_q()).order_by("created_at")


def propose(schedule):
    """Create a pending batch for the schedule, or return None when nothing is due."""
    from core.models import DispositionBatch, DispositionItem
    if not schedule.active or schedule.batches.filter(status="pending").exists():
        return None
    due = list(candidates(schedule).values("pk", "filename", "project_id", "created_at")[:MAX_BATCH])
    if not due:
        return None
    with transaction.atomic():
        batch = DispositionBatch.objects.create(client_id=schedule.client_id, schedule=schedule)
        DispositionItem.objects.bulk_create([
            DispositionItem(batch=batch, file_id=d["pk"], file_ref=d["pk"], filename=d["filename"][:255],
                            project_id=d["project_id"] or "", created=d["created_at"]) for d in due])
    logger.info("Disposition batch %s proposed by schedule %s (%d documents)", batch.pk, schedule.pk, len(due))
    return batch


def carry_out(batch, user):
    """Approve: delete each document through the safe path, keeping held or checked-out ones."""
    from core.checkout import holder
    from core.holds import DocumentOnHold, is_held
    from core.models import File
    from document_operations.utils import _delete_link, _remove_unused_bytes
    counts = {}
    for item in batch.items.select_related("file").filter(outcome="pending"):
        doc = item.file
        if doc is None or not File.objects.filter(pk=item.file_ref).exists():
            item.outcome = "missing"
        elif is_held(doc):
            item.outcome = "held"
        elif holder(doc):
            item.outcome = "checked_out"
        else:
            try:
                paths = set()
                with transaction.atomic():
                    for link in list(doc.folder_links.all()):
                        paths.add(_delete_link(link))
                    if File.objects.filter(pk=doc.pk).exists():   # never linked to a folder
                        paths.add(doc.filepath)
                        doc.delete()
                    transaction.on_commit(lambda p=paths: _remove_unused_bytes(p))
                item.outcome = "deleted"
            except DocumentOnHold:
                item.outcome = "held"
        # The document may be gone; record the outcome without touching the (deleted) related object.
        type(item).objects.filter(pk=item.pk).update(outcome=item.outcome)
        counts[item.outcome] = counts.get(item.outcome, 0) + 1
    batch.status, batch.decided_by, batch.decided_at = "approved", user, timezone.now()
    batch.save(update_fields=["status", "decided_by", "decided_at"])
    logger.info("Disposition batch %s approved by user %s: %s", batch.pk, user.pk, counts)
    return counts


def reject(batch, user, note=""):
    batch.items.filter(outcome="pending").update(outcome="kept")
    batch.status, batch.decided_by, batch.decided_at, batch.note = "rejected", user, timezone.now(), note[:5000]
    batch.save(update_fields=["status", "decided_by", "decided_at", "note"])


@shared_task(name="core.retention_review", ignore_result=True, soft_time_limit=600, time_limit=660)
def retention_review():
    """Daily: propose disposition batches for every active schedule. Never deletes anything."""
    from core.models import RetentionSchedule
    for schedule in RetentionSchedule.objects.filter(active=True).select_related("matter"):
        try:
            propose(schedule)
        except Exception:
            logger.exception("Retention review failed for schedule %s", schedule.pk)
