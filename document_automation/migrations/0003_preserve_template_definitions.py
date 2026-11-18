from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def copy_source_owners(apps, schema_editor):
    Template = apps.get_model("document_automation", "AutomationTemplate")
    for template in Template.objects.select_related("file").filter(owner__isnull=True).iterator(chunk_size=500):
        if template.file_id:
            Template.objects.filter(pk=template.pk).update(owner_id=template.file.user_id)


class Migration(migrations.Migration):
    dependencies = [
        ("document_automation", "0002_automationresult_generation_index_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.AddField(
            model_name="automationtemplate", name="owner",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="automation_template_definitions", to=settings.AUTH_USER_MODEL),
        ),
        migrations.RunPython(copy_source_owners, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="automationtemplate", name="file",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="automation_templates", to="core.file"),
        ),
    ]
