"""
Housekeeping that keeps the platform from getting stuck.

* fail_stale_work: a document job whose worker died (out of memory, restart, queue
  purge) used to stay "Processing" forever, so people waited on work that would
  never finish. Anything that has shown no progress for longer than a task is
  allowed to run is marked Failed with a message the user can act on.
* purge_expired_credentials: expired access tokens, used password-reset tokens
  and old sign-in codes are deleted so they cannot be replayed and tables stay small.
* queue_report / purge_queues: what is waiting in each queue, and a safe way to
  empty them (used by `manage.py queue_doctor`).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.apps import apps
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

STALE_MESSAGE = ("This job stopped before it finished (the processing service was restarted or ran out "
                 "of memory). Please run it again.")

# (app label, model, statuses that mean "still running")
TRACKED_WORK = [
    ("document_ocr", "OCRRun", ("Pending", "Processing")),
    ("document_ocr", "OCRFile", ("Pending", "Processing")),
    ("document_translation", "TranslationRun", ("Pending", "Processing")),
    ("document_translation", "TranslationFile", ("Pending", "Processing")),
    ("document_anonymizer", "AnonymizationRun", ("Pending", "Processing")),
    ("document_anonymizer", "Anonymize", ("Pending", "Processing")),
    ("document_redlining", "RedliningRun", ("Pending", "Processing")),
    ("document_automation", "AutomationRun", ("Pending", "Processing")),
    ("document_classification", "ClusteringRun", ("Pending", "Processing")),
    ("document_structures", "DocumentStructureRun", ("Pending", "Processing")),
    ("core", "BulkJob", ("Pending", "Queued", "Processing")),
    ("core", "BulkJobFileResult", ("Pending", "Queued", "Processing")),
]


def _field_names(model) -> set:
    return {f.name for f in model._meta.get_fields() if getattr(f, "concrete", False)}


def fail_stale(older_than_seconds: int | None = None, everything: bool = False) -> dict:
    """Mark unfinished work as Failed. `everything=True` ignores age (used after a queue purge)."""
    seconds = settings.STALE_RUN_AFTER_SECONDS if older_than_seconds is None else older_than_seconds
    cutoff = timezone.now() - timedelta(seconds=seconds)
    report = {}
    for app_label, model_name, running in TRACKED_WORK:
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            continue
        fields = _field_names(model)
        if "status" not in fields:
            continue
        qs = model.objects.filter(status__in=running)
        if not everything:
            stamp = "updated_at" if "updated_at" in fields else ("created_at" if "created_at" in fields else None)
            if stamp is None:
                continue
            qs = qs.filter(**{f"{stamp}__lt": cutoff})
        changes = {"status": "Failed"}
        if "error_message" in fields:
            changes["error_message"] = STALE_MESSAGE
        if "updated_at" in fields:
            changes["updated_at"] = timezone.now()
        if "completed_at" in fields:
            changes["completed_at"] = timezone.now()
        try:
            count = qs.update(**changes)
        except Exception:
            logger.exception("Could not mark stale %s.%s rows as failed", app_label, model_name)
            continue
        if count:
            report[f"{app_label}.{model_name}"] = count
    if report:
        logger.warning("Marked unfinished work as failed: %s", report)
    return report


@shared_task(name="core.maintenance.fail_stale_work", ignore_result=True, soft_time_limit=120, time_limit=180)
def fail_stale_work():
    return fail_stale()


@shared_task(name="core.maintenance.purge_expired_credentials", ignore_result=True, soft_time_limit=300, time_limit=360)
def purge_expired_credentials():
    from oauth2_provider.models import AccessToken
    from custom_authentication.models import PasswordResetToken, TwoFactorCode

    now = timezone.now()
    out = {
        "access_tokens": AccessToken.objects.filter(expires__lt=now - timedelta(days=1)).delete()[0],
        "reset_tokens": PasswordResetToken.objects.filter(created_at__lt=now - timedelta(days=1)).delete()[0],
        "sign_in_codes": TwoFactorCode.objects.filter(created_at__lt=now - timedelta(days=1)).delete()[0],
    }
    logger.info("Expired credentials removed: %s", out)
    return out


# ── queues ───────────────────────────────────────────────────────────────────

def _queue_names() -> list:
    names = [settings.CELERY_TASK_DEFAULT_QUEUE, getattr(settings, "CELERY_FAST_QUEUE", "")]
    return [n for n in dict.fromkeys(names) if n]


def _broker():
    import redis

    return redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=3, socket_timeout=10)


def _owned_reserved(r):
    """Kombu shares the unacked hash across queues; never touch another app's jobs."""
    import json
    queues = set(_queue_names())
    for tag, payload in r.hscan_iter("unacked"):
        try:
            _, exchange, routing_key = json.loads(payload)
        except (ValueError, TypeError):
            continue
        if routing_key in queues or exchange in queues:
            yield tag


def queue_report() -> dict:
    """Waiting and reserved tasks belonging to this platform, and Redis memory."""
    r = _broker()
    info = r.info("memory")
    return {
        "queues": {name: r.llen(name) for name in _queue_names()},
        "reserved_by_workers": sum(1 for _ in _owned_reserved(r)),
        "redis_used_memory": info.get("used_memory_human"),
        "redis_maxmemory": info.get("maxmemory_human"),
    }


def purge_queues(include_reserved: bool = True) -> dict:
    """Purge this platform's queues without deleting foreign Kombu reservations."""
    r = _broker()
    removed = {name: r.llen(name) for name in _queue_names()}
    for name in _queue_names():
        r.delete(name)
    if include_reserved:
        tags = list(_owned_reserved(r))
        removed["reserved_by_workers"] = len(tags)
        if tags:
            pipeline = r.pipeline(transaction=True)
            pipeline.hdel("unacked", *tags)
            pipeline.zrem("unacked_index", *tags)
            pipeline.execute()
    removed["marked_failed"] = fail_stale(everything=True)
    logger.warning("Queues purged: %s", removed)
    return removed
