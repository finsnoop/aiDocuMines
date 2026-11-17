from django.db import migrations
from django.utils import timezone


def invalidate_saved_text(apps, schema_editor):
    # Before the editor-save fix, these bytes could have newer contents than the
    # stored text. Only derived extraction is discarded; saved files are retained.
    File = apps.get_model('core', 'File')
    File.objects.filter(status='Saved').update(content='', updated_at=timezone.now())


class Migration(migrations.Migration):
    dependencies = [('core', '0014_scale_indexes')]
    operations = [migrations.RunPython(invalidate_saved_text, migrations.RunPython.noop)]
