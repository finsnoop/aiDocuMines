from django.db import migrations
from core.fields.encrypted_char import EncryptedCharField

SECRET_FIELDS = ('openai_api_key', 'azure_storage_key', 'oauth_client_secret', 'slack_webhook')


def encrypt_existing(apps, schema_editor):
    Settings = apps.get_model('system_settings', 'SystemSettings')
    Audit = apps.get_model('system_settings', 'SystemSettingsAuditTrail')
    for row in Settings.objects.all().iterator(chunk_size=200):
        # Legacy plaintext remains readable by the encrypted field during conversion.
        row.save(update_fields=SECRET_FIELDS)
    for row in Audit.objects.all().iterator(chunk_size=200):
        if isinstance(row.changes, dict) and any(key in row.changes for key in SECRET_FIELDS):
            row.changes = {key: '********' if key in SECRET_FIELDS else value for key, value in row.changes.items()}
            row.save(update_fields=['changes'])


class Migration(migrations.Migration):
    dependencies = [('system_settings', '0002_add_platform_global_settings_and_llm_defaults')]
    operations = [
        migrations.AlterField('systemsettings', field,
                              EncryptedCharField(max_length=1024, blank=True, null=True))
        for field in SECRET_FIELDS
    ] + [migrations.RunPython(encrypt_existing)]
