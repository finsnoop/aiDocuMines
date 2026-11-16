# file_monitor/management/commands/monitor_files.py

import os
import time
from django.core.management.base import BaseCommand
from watchdog.observers import Observer
from file_monitor.handlers import FileActivityHandler
from django.conf import settings


class Command(BaseCommand):
    help = "Starts the watchdog observer to monitor file activity in the media/uploads directory."

    def handle(self, *args, **options):
        watch_path = os.path.join(settings.MEDIA_ROOT, "uploads")

        if not os.path.exists(watch_path):
            os.makedirs(watch_path)
            self.stdout.write(self.style.WARNING(f"Created watch directory: {watch_path}"))

        self.stdout.write(self.style.SUCCESS(f"Starting file activity monitor on: {watch_path}"))

        event_handler = FileActivityHandler()
        observer = Observer()
        observer.schedule(event_handler, path=watch_path, recursive=True)
        observer.start()

        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            observer.stop()
            self.stdout.write(self.style.WARNING("File monitor interrupted by user."))

        observer.join()
        self.stdout.write(self.style.SUCCESS("File activity monitor stopped."))

