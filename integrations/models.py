# integrations/models.py

from django.db import models
from django.contrib.auth import get_user_model

from core.fields.encrypted_char import EncryptedCharField

User = get_user_model()

class IntegrationLog(models.Model):
    STATUS_CHOICES = [
        ("created", "User Created"),
        ("reset", "Password Reset"),
        ("failed", "Failed"),
        ("error", "Error"),
        ("processing", "Processing"),
        ("skipped", "Skipped"),
        ("success", "Success"),
    ]

    CONNECTOR_CHOICES = [
        ("nextcloud", "Nextcloud"),
        # Add more like ("mattermost", "Mattermost") etc. if needed
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    connector = models.CharField(max_length=50, choices=CONNECTOR_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    details = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    autologin_url = models.URLField(blank=True, null=True)  # Optional: store link used

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.user} - {self.connector} [{self.status}]"


class NextcloudConnection(models.Model):
    CONNECTION_TYPE_CHOICES = [
        ("managed", "Managed (auto-provisioned)"),
        ("custom", "Custom (user-provided credentials)"),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="nextcloud_connection")
    connection_type = models.CharField(max_length=10, choices=CONNECTION_TYPE_CHOICES, default="managed")

    # Custom connection details (used when connection_type = "custom")
    custom_server_url = models.URLField(blank=True, null=True, help_text="Custom Nextcloud server URL")
    custom_username = models.CharField(max_length=255, blank=True, null=True, help_text="Username on the custom Nextcloud server")
    custom_password = models.CharField(max_length=512, blank=True, null=True, help_text="App password for the custom Nextcloud server")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Nextcloud Connection"
        verbose_name_plural = "Nextcloud Connections"

    def __str__(self):
        return f"{self.user.email} — {self.connection_type}"


class ConnectorEndpointRequest(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="connector_requests")
    source = models.CharField(max_length=80)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "source"], name="uniq_user_connector_request")
        ]

    def __str__(self):
        return f"{self.user_id} requested {self.source}"



class ConnectorAccount(models.Model):
    """One user's connection to an external service (OAuth tokens, or a webhook for Zapier / Make).

    Secrets are encrypted at rest (AES-GCM, ENCRYPTION_SECRET_KEY) and never returned by the API.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="connector_accounts")
    service = models.CharField(max_length=40)
    account_label = models.CharField(max_length=255, blank=True, default="")   # e.g. the connected email
    access_token = EncryptedCharField(max_length=8000, blank=True, default="")
    refresh_token = EncryptedCharField(max_length=8000, blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True)
    scopes = models.TextField(blank=True, default="")
    extra = models.JSONField(default=dict, blank=True)        # non-secret: instance URL, site ids, webhook URL
    webhook_secret = EncryptedCharField(max_length=512, blank=True, default="")   # signs outbound events
    inbound_token_hash = models.CharField(max_length=64, blank=True, default="", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "service"], name="uniq_user_connector_account")]

    def __str__(self):
        return f"{self.user_id} -> {self.service}"


class ConnectorJob(models.Model):
    """An import or export between aiDocuMines and a connector, run in the background."""
    KIND_CHOICES = [("import", "Import"), ("export", "Export")]
    STATUS_CHOICES = [("queued", "Queued"), ("running", "Running"), ("done", "Done"), ("failed", "Failed")]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="connector_jobs")
    service = models.CharField(max_length=40)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="queued")
    request = models.JSONField(default=dict)
    results = models.JSONField(default=list)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]


class ESignConnection(models.Model):
    """An organisation's connection to a self-hosted (or open-source) e-signature service: DocuSeal, Documenso or
    SignuDoc. Set by the organisation's administrator; every sender in the organisation uses it (unlike DocuSign
    and Adobe Sign, which use each sender's own account). The API token and webhook secret are encrypted."""
    PROVIDERS = [("docuseal", "DocuSeal"), ("documenso", "Documenso"), ("signudoc", "SignuDoc")]
    client = models.ForeignKey("custom_authentication.Client", on_delete=models.CASCADE, related_name="esign_connections")
    provider = models.CharField(max_length=20, choices=PROVIDERS)
    base_url = models.CharField(max_length=300)                    # the service's API address
    api_token = EncryptedCharField(max_length=2000, blank=True, default="")
    webhook_secret = EncryptedCharField(max_length=512, blank=True, default="")   # from the provider, to verify callbacks
    webhook_token = models.CharField(max_length=64, db_index=True)  # unguessable part of our callback address
    enabled = models.BooleanField(default=True)
    updated_by = models.ForeignKey(get_user_model(), null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["client", "provider"], name="one_esign_connection_per_provider")]

    def __str__(self):
        return f"{self.get_provider_display()} for client {self.client_id}"
