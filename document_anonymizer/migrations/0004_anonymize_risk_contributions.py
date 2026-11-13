from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('document_anonymizer', '0003_anonymizationrun_client'),
    ]

    operations = [
        migrations.AddField(
            model_name='anonymize',
            name='risk_contributions',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
