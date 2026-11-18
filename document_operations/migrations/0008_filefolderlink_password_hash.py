from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("document_operations", "0007_repair_storage_path_folders")]
    operations = [migrations.AddField(model_name="filefolderlink", name="password_hash",
                                     field=models.CharField(blank=True, default="", max_length=128))]
