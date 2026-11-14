from __future__ import annotations

from celery import shared_task
from .intelligence import build_intelligence  # Register the bounded dashboard agent task.
from django.utils import timezone
from datetime import datetime
from typing import Optional

from .utils import gather_user_dashboard_metrics
from .models import HomeAnalyticsSnapshot


@shared_task(bind=True)
def compute_home_analytics(self, user_id: int, since_iso: Optional[str] = None):
    """
    Celery task that computes the analytics payload and stores/updates a snapshot.
    """
    since = None
    if since_iso:
        try:
            since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        except Exception:
            since = None

    payload = gather_user_dashboard_metrics(user_id=user_id, since=since_iso)
    obj, _ = HomeAnalyticsSnapshot.objects.update_or_create(
        user_id=user_id,
        window_since=since,
        defaults={
            "payload": payload,
            "generated_at": timezone.now(),
            "generated_async": True,
            "task_id": self.request.id,
        },
    )
    return payload

