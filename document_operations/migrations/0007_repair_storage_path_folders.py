"""Re-file documents that were linked under server storage folders (app/media/uploads/...)."""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)


def repair(apps, schema_editor):
    try:
        from document_operations.utils import repair_misplaced_folder_links
        moved, deleted = repair_misplaced_folder_links()
        logger.info("Folder tree repair: re-filed %s documents, removed %s folders", moved, deleted)
    except Exception:  # never block a deployment; `manage.py repair_folder_tree` can be rerun
        logger.exception("Folder tree repair skipped")


class Migration(migrations.Migration):
    dependencies = [
        ("document_operations", "0006_filecolortag_foldercolortag_filealias_folderalias"),
        ("core", "0012_scale_indexes"),
    ]
    operations = [migrations.RunPython(repair, migrations.RunPython.noop)]
