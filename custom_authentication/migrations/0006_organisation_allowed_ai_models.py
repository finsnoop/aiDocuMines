from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("custom_authentication", "0005_organisation_ai_access"),
    ]

    operations = [
        migrations.AddField(
            model_name="organisationpolicy", name="allowed_ai_models",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
