from __future__ import annotations
from django.conf import settings
from django.db import models
import json
import uuid


class IntelligenceRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    status = models.CharField(max_length=24, default='queued')
    file_ids = models.JSONField(default=list)
    completed = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class DocumentIntelligence(models.Model):
    file = models.OneToOneField('core.File', on_delete=models.CASCADE, related_name='business_intelligence')
    tenant = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE)
    source_hash = models.CharField(max_length=64)
    source_updated_at = models.DateTimeField(null=True)
    facts = models.JSONField(default=dict)
    provider = models.CharField(max_length=32)
    model = models.CharField(max_length=255)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class IntelligenceRule(models.Model):
    tenant = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE)
    name = models.CharField(max_length=120)
    instruction = models.TextField(max_length=4000)
    document_category = models.CharField(max_length=80, blank=True)
    enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)


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


# ── Agent operations: event log, model prices, budgets ───────────────────────────────────────────
class AgentEvent(models.Model):
    """Immutable record of every decision the agents take (dispatch, pause, budget, capacity)."""
    SEVERITIES = [('info', 'Info'), ('warning', 'Warning'), ('critical', 'Critical')]
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    agent = models.CharField(max_length=32, db_index=True)   # orchestrator, resource, cost, intelligence, ocr, similarity
    kind = models.CharField(max_length=64, db_index=True)
    severity = models.CharField(max_length=12, choices=SEVERITIES, default='info')
    tenant = models.ForeignKey('custom_authentication.Client', null=True, blank=True, on_delete=models.CASCADE)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['tenant', '-created_at'])]


class ModelPrice(models.Model):
    """Provider price per million tokens, maintained by platform administrators.

    No prices are built in: provider prices change, and an invented price is worse than "unpriced".
    provider '*' applies to the model through any provider (e.g. via LiteLLM).
    """
    provider = models.CharField(max_length=32, default='*')
    model = models.CharField(max_length=255)
    input_usd_per_million = models.DecimalField(max_digits=12, decimal_places=6)
    output_usd_per_million = models.DecimalField(max_digits=12, decimal_places=6)
    source = models.CharField(max_length=255, blank=True, help_text='Where the price came from, e.g. the provider pricing page and date')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('provider', 'model')]


class AIBudget(models.Model):
    """Monthly AI spend limit for one organisation."""
    tenant = models.OneToOneField('custom_authentication.Client', on_delete=models.CASCADE, related_name='ai_budget')
    monthly_usd = models.DecimalField(max_digits=12, decimal_places=2)
    alert_percent = models.PositiveSmallIntegerField(default=80)
    hard_stop = models.BooleanField(default=True, help_text='Stop agent calls once the monthly budget is used up')
    updated_at = models.DateTimeField(auto_now=True)


class AISpendLimit(models.Model):
    """A spend limit on one person or one AI key in an organisation, per day, week or month.

    Switched on and off with `enabled`; an enabled limit that is used up refuses further AI
    calls for that person or key until the next period (see agents/gateway.py).
    """
    SCOPES = [('user', 'Person'), ('key', 'AI key')]
    PERIODS = [('day', 'Day'), ('week', 'Week'), ('month', 'Month')]
    UNITS = [('usd', 'US dollars'), ('tokens', 'Tokens')]
    tenant = models.ForeignKey('custom_authentication.Client', on_delete=models.CASCADE, related_name='ai_spend_limits')
    scope = models.CharField(max_length=8, choices=SCOPES)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name='ai_spend_limits')
    key_id = models.CharField(max_length=64, blank=True, default='')
    period = models.CharField(max_length=8, choices=PERIODS, default='month')
    unit = models.CharField(max_length=8, choices=UNITS, default='usd')
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    enabled = models.BooleanField(default=True)
    alert_percent = models.PositiveSmallIntegerField(default=80)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'user'], condition=models.Q(scope='user'), name='ai_limit_one_per_user'),
            models.UniqueConstraint(fields=['tenant', 'key_id'], condition=models.Q(scope='key'), name='ai_limit_one_per_key'),
        ]
        indexes = [models.Index(fields=['tenant', 'enabled'])]
