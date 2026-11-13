from django.db import migrations, models

import core.fields.encrypted_char


class Migration(migrations.Migration):

    dependencies = [
        ("custom_authentication", "0004_organisation_policy_and_audit"),
    ]

    operations = [
        migrations.AddField(
            model_name="organisationpolicy", name="ai_provider",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="organisationpolicy", name="ai_api_key",
            field=core.fields.encrypted_char.EncryptedCharField(blank=True, default="", max_length=1000),
        ),
        migrations.AddField(
            model_name="organisationpolicy", name="allow_personal_ai_keys",
            field=models.BooleanField(default=False),
        ),
    ]
