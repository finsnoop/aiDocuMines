"""
See what is waiting in the task queues and, when they are jammed, clear them safely.

    python manage.py queue_doctor                 # report only
    python manage.py queue_doctor --fail-stale    # mark work with no progress as Failed
    python manage.py queue_doctor --purge         # drop all waiting tasks + mark their work Failed

`--purge` is what to run when a restart did not help: old tasks survive restarts because
Redis keeps the queue on disk. Purging removes them; people simply run their job again.
"""
import json

from django.core.management.base import BaseCommand

from core import maintenance


class Command(BaseCommand):
    help = "Report on, and optionally clear, the Celery task queues."

    def add_arguments(self, parser):
        parser.add_argument("--purge", action="store_true", help="Drop every waiting task and mark its work Failed.")
        parser.add_argument("--fail-stale", action="store_true", help="Mark work with no recent progress as Failed.")
        parser.add_argument("--keep-reserved", action="store_true",
                            help="With --purge: keep tasks currently held by a worker.")

    def handle(self, *args, **opts):
        self.stdout.write("Before: " + json.dumps(maintenance.queue_report(), default=str))
        if opts["purge"]:
            result = maintenance.purge_queues(include_reserved=not opts["keep_reserved"])
            self.stdout.write(self.style.WARNING("Purged: " + json.dumps(result, default=str)))
        elif opts["fail_stale"]:
            self.stdout.write("Marked failed: " + json.dumps(maintenance.fail_stale(), default=str))
        if opts["purge"] or opts["fail_stale"]:
            self.stdout.write("After:  " + json.dumps(maintenance.queue_report(), default=str))
