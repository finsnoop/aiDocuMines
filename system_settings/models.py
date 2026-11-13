# system_settings/models.py
from django.db import models
from django.conf import settings
from core.models import Run
from custom_authentication.models import Client


class SystemSettings(models.Model):
    client = models.OneToOneField('custom_authentication.Client', on_delete=models.CASCADE, related_name='system_settings')

    # PLATFORM CONFIG
    site_name = models.CharField(max_length=255, default="AI DocuMines")
    default_language = models.CharField(max_length=10, choices=[("en", "English"), ("fr", "French")], default="en")
    dark_mode = models.BooleanField(default=True)
    maintenance_mode = models.BooleanField(default=False)
    public_registration = models.BooleanField(default=True)

    # SECURITY
    password_pattern = models.CharField(max_length=255, blank=True, null=True)
    password_expiry_days = models.PositiveIntegerField(default=90)
    session_timeout = models.PositiveIntegerField(default=30)
    enforce_2fa = models.BooleanField(default=False)
    email_verification = models.BooleanField(default=True)
    api_rate_limit = models.PositiveIntegerField(default=60)
    disable_file_downloads = models.BooleanField(default=False)

    # DATA PROTECTION
    enable_gdpr_mode = models.BooleanField(default=True)
    log_data_access = models.BooleanField(default=True)
    redact_logs = models.BooleanField(default=False)

    # EMAIL
    smtp_host = models.CharField(max_length=255, blank=True, null=True)
    smtp_port = models.PositiveIntegerField(default=587)
    from_email = models.EmailField(blank=True, null=True)
    admin_email = models.EmailField(blank=True, null=True)
    notify_enabled = models.BooleanField(default=True)

    # INTEGRATIONS
    openai_api_key = models.CharField(max_length=512, blank=True, null=True)
    azure_storage_key = models.CharField(max_length=512, blank=True, null=True)
    slack_webhook = models.URLField(blank=True, null=True)
    oauth_client_id = models.CharField(max_length=255, blank=True, null=True)
    oauth_client_secret = models.CharField(max_length=512, blank=True, null=True)

    # BACKUP
    backup_frequency = models.CharField(max_length=10, choices=[("daily", "Daily"), ("weekly", "Weekly"), ("monthly", "Monthly")], default="daily")
    backup_retention = models.PositiveIntegerField(default=30)
    encrypt_backups = models.BooleanField(default=True)

    # LOGGING
    enable_logging = models.BooleanField(default=True)
    log_retention = models.PositiveIntegerField(default=90)
    log_level = models.CharField(max_length=10, choices=[("debug", "Debug"), ("info", "Info"), ("warn", "Warning"), ("error", "Error")], default="info")
    send_logs_to_admin = models.BooleanField(default=False)

    # USER MANAGEMENT
    default_role = models.CharField(max_length=50, default="User")
    disable_self_delete = models.BooleanField(default=False)
    max_users = models.PositiveIntegerField(default=100)
    require_role_approval = models.BooleanField(default=True)

    # FILE POLICIES
    allow_file_upload = models.BooleanField(default=True)
    max_upload_mb = models.PositiveIntegerField(default=100)
    allowed_file_types = models.CharField(max_length=255, default=".pdf,.docx,.xlsx")
    default_timezone = models.CharField(max_length=100, default="UTC")

    # COMPLIANCE
    enable_iso_logging = models.BooleanField(default=False)
    enable_hipaa_mode = models.BooleanField(default=False)
    enable_audit_log_download = models.BooleanField(default=True)

    # AI / LLM DEFAULTS (per-client override — falls back to PlatformGlobalSettings)
    default_llm_provider = models.CharField(max_length=50, blank=True, null=True, help_text="litellm | openai | ollama | vllm — empty means use platform global")
    default_llm_model = models.CharField(max_length=255, blank=True, null=True, help_text="Model name — empty means use platform global")
    default_llm_temperature = models.FloatField(default=0.3, help_text="Default temperature for LLM calls")
    default_llm_max_tokens = models.PositiveIntegerField(default=4096, help_text="Default max tokens for LLM calls")

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Settings for {self.client.name}"


class PlatformGlobalSettings(models.Model):
    """
    Singleton model for platform-wide global defaults (superadmin only).
    There should only ever be one row.  The admin UI uses get_or_create(pk=1).
    """
    site_name = models.CharField(max_length=255, default="AI DocuMines")
    default_language = models.CharField(max_length=10, choices=[("en", "English"), ("fr", "French")], default="en")
    default_timezone = models.CharField(max_length=100, default="UTC")
    maintenance_mode = models.BooleanField(default=False)

    # AI / LLM GLOBAL DEFAULTS
    default_llm_provider = models.CharField(
        max_length=50, default="litellm",
        help_text="Default LLM provider: litellm | openai | ollama | vllm"
    )
    default_llm_model = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Default model name.  If empty, queries LiteLLM for the first available."
    )
    default_llm_temperature = models.FloatField(default=0.3)
    default_llm_max_tokens = models.PositiveIntegerField(default=4096)

    # SECURITY
    enforce_2fa = models.BooleanField(default=False)
    session_timeout = models.PositiveIntegerField(default=30)
    api_rate_limit = models.PositiveIntegerField(default=60)

    # FILE POLICIES
    max_upload_mb = models.PositiveIntegerField(default=100)

    # COMPLIANCE
    enable_gdpr_mode = models.BooleanField(default=True)
    enable_hipaa_mode = models.BooleanField(default=False)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Platform Global Settings"
        verbose_name_plural = "Platform Global Settings"

    def __str__(self):
        return f"Platform Global Settings ({self.site_name})"

    def save(self, *args, **kwargs):
        # Enforce singleton
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class SystemSettingsAuditTrail(models.Model):
    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    run = models.ForeignKey(Run, on_delete=models.SET_NULL, null=True, blank=True)
    changes = models.JSONField()
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        verbose_name = "System Settings Audit Trail"
        verbose_name_plural = "System Settings Audit Trails"

    def __str__(self):
        return f"Audit for {self.client.name} at {self.timestamp}"

