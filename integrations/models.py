# integrations/models.py

from django.db import models
from django.contrib.auth import get_user_model

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

