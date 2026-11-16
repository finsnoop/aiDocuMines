import os
from celery import Celery

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aiDocuMines.settings")

app = Celery("aiDocuMines")

# Load task modules from all registered Django app configs.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Autodiscover tasks
app.autodiscover_tasks()
app.autodiscover_tasks(related_name="maintenance")  # core.maintenance (stale-work sweeper)
app.autodiscover_tasks(related_name="agents")  # home_dash_analytics.agents (orchestrator, resource, cost)

# AI calls made inside a task are recorded against the task's user_id (agents/usage.py).
from home_dash_analytics.agents.usage import connect_celery_signals  # noqa: E402
connect_celery_signals()

# Liveness: the worker refreshes a file every 15 seconds; the container health check only
# looks at the file's age. (The previous check, `celery inspect ping`, loaded a second copy
# of the whole application every 30 seconds.)
from celery import bootsteps  # noqa: E402

LIVENESS_FILE = os.environ.get("CELERY_LIVENESS_FILE", "/tmp/celery_worker_alive")


def _touch_liveness():
    try:
        with open(LIVENESS_FILE, "w") as fh:
            fh.write("ok")
    except OSError:
        pass


class LivenessProbe(bootsteps.StartStopStep):
    requires = {"celery.worker.components:Timer"}

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.tref = None

    def start(self, parent):
        _touch_liveness()
        self.tref = parent.timer.call_repeatedly(15.0, _touch_liveness, priority=10)

    def stop(self, parent):
        if self.tref is not None:
            self.tref.cancel()
            self.tref = None
        try:
            os.remove(LIVENESS_FILE)
        except OSError:
            pass


app.steps["worker"].add(LivenessProbe)


@app.task(bind=True)
def debug_task(self):
    print(f"Request: {self.request!r}")
    
# print("Celery Tasks Discovered:", app.tasks.keys())  # Add this debug line
