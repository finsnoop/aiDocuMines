from __future__ import annotations
from django.conf import settings
from django.db import models


class HomeAnalyticsSnapshot(models.Model):
    """
    Stores a cached/snapshotted analytics payload for a given user and window.
    """
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="home_dash_snapshots")
    window_since = models.DateTimeField(null=True, blank=True)
    payload = models.JSONField()
    generated_at = models.DateTimeField(auto_now_add=True)
    generated_async = models.BooleanField(default=False)
    task_id = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "-generated_at"]),
        ]
        ordering = ["-generated_at"]

    def __str__(self) -> str:
        return f"HomeAnalyticsSnapshot(user={self.user_id}, at={self.generated_at:%Y-%m-%d %H:%M})"



class DashboardLayout(models.Model):
    """A user's dashboard widget layout and UI preferences, optionally per matter/service."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="dashboard_layouts")
    project_id = models.CharField(max_length=255, blank=True, default="")
    service_id = models.CharField(max_length=255, blank=True, default="")
    layout = models.JSONField(default=list, blank=True)
    ui_prefs = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "project_id", "service_id"], name="uniq_dashboard_layout_scope"),
        ]

    def __str__(self) -> str:
        return f"DashboardLayout(user={self.user_id}, project={self.project_id or '-'}, service={self.service_id or '-'})"
