import uuid

from django.conf import settings
from django.db import models


class EvalRun(models.Model):
    """One run of the AI evaluation suite against a model, with its scores (ai_eval/scoring.py)."""
    SUITES = [("extraction", "Document facts"), ("playbook", "Playbook review"), ("assistant", "Ask Assistant")]
    STATUSES = [("pending", "Queued"), ("running", "Running"), ("done", "Done"), ("failed", "Failed")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    suite = models.CharField(max_length=12, choices=SUITES)
    dataset = models.CharField(max_length=60, blank=True, default="")
    provider = models.CharField(max_length=40, blank=True, default="")
    model = models.CharField(max_length=255, blank=True, default="")
    case_limit = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=8, choices=STATUSES, default="pending")
    metrics = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    started_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
