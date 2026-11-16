from watchdog.events import FileSystemEventHandler
from core.models import File, User
from file_monitor.models import FileEventLog
from django.utils.timezone import now
from django.conf import settings
import os

class FileActivityHandler(FileSystemEventHandler):
    def log_event(self, filepath, event_type):
        try:
            relative_path = filepath.replace(settings.MEDIA_ROOT + "/", "")
            print(f"Relative path to match: {relative_path}")
    
            file_record = File.objects.filter(filepath=filepath).first()
            print(f"Matched file: {file_record}")
    
            if not file_record:
                print(f"No File entry found in DB matching path: {relative_path}")
                return  # Skip logging since there's no matching file
    
            FileEventLog.objects.create(
                file=file_record,
                event_type=event_type,
                path=filepath,
                timestamp=now(),
                triggered_by=file_record.user if file_record else None,
                details={"detected_by": "watchdog"},
                notes=f"Auto-logged via event: {event_type}"
            )
    
            print(f"Logged {event_type.upper()} event for: {filepath}")
    
        except Exception as e:
            print(f"Error logging event for {filepath}: {e}")
    
    def on_modified(self, event):
        if not event.is_directory:
            self.log_event(event.src_path, "modified")

    def on_created(self, event):
        if not event.is_directory:
            self.log_event(event.src_path, "created")

    def on_deleted(self, event):
        if not event.is_directory:
            self.log_event(event.src_path, "deleted")
