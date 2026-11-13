"""
Move documents that were filed under server storage folders (app/media/uploads/...)
back under <matter>/<service>/<date>, and remove the empty storage-path folders.

    python manage.py repair_folder_tree
"""
from django.core.management.base import BaseCommand

from document_operations.utils import repair_misplaced_folder_links


class Command(BaseCommand):
    help = "Re-file documents shown under server storage paths in the folder tree."

    def handle(self, *args, **opts):
        moved, deleted = repair_misplaced_folder_links()
        self.stdout.write(self.style.SUCCESS(f"Re-filed {moved} document(s); removed {deleted} storage-path folder(s)."))
