# file_monitor/watcher.py
import time
from watchdog.observers import Observer
from .handlers import FileActivityHandler
from django.conf import settings
import os

def start_monitor():
    event_handler = FileActivityHandler()
    observer = Observer()

    watch_path = os.path.join(settings.MEDIA_ROOT, "uploads")
    observer.schedule(event_handler, watch_path, recursive=True)

    observer.start()
    print(f"Monitoring started on: {watch_path}")
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

