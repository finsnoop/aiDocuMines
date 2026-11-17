import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import core.fields.encrypted_char


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0004_connectorendpointrequest"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ConnectorAccount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("service", models.CharField(max_length=40)),
                ("account_label", models.CharField(blank=True, default="", max_length=255)),
                ("access_token", core.fields.encrypted_char.EncryptedCharField(blank=True, default="", max_length=8000)),
                ("refresh_token", core.fields.encrypted_char.EncryptedCharField(blank=True, default="", max_length=8000)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("scopes", models.TextField(blank=True, default="")),
                ("extra", models.JSONField(blank=True, default=dict)),
                ("webhook_secret", core.fields.encrypted_char.EncryptedCharField(blank=True, default="", max_length=512)),
                ("inbound_token_hash", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="connector_accounts", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddConstraint(
            model_name="connectoraccount",
            constraint=models.UniqueConstraint(fields=("user", "service"), name="uniq_user_connector_account"),
        ),
        migrations.CreateModel(
            name="ConnectorJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("service", models.CharField(max_length=40)),
                ("kind", models.CharField(choices=[("import", "Import"), ("export", "Export")], max_length=10)),
                ("status", models.CharField(choices=[("queued", "Queued"), ("running", "Running"), ("done", "Done"), ("failed", "Failed")], default="queued", max_length=10)),
                ("request", models.JSONField(default=dict)),
                ("results", models.JSONField(default=list)),
                ("error", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="connector_jobs", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
