"""Bind asynchronous results to the caller; unknown and expired bindings fail closed."""
from django.conf import settings
from django.core.cache import cache
from django.http import Http404


def register_task(user, task, kind):
    timeout = int(getattr(settings, "CELERY_RESULT_EXPIRES", 86400))
    cache.set(f"owned_task:{kind}:{task.id}", user.pk, timeout=timeout)
    return task


def require_task_owner(user, task_id, kind):
    if cache.get(f"owned_task:{kind}:{task_id}") != user.pk:
        raise Http404
