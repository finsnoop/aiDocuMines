"""Bind asynchronous results to the caller; unknown and expired bindings fail closed."""
from django.conf import settings
from django.core.cache import cache
from django.http import Http404


def register_task(user, task, kind):
    timeout = int(getattr(settings, "CELERY_RESULT_EXPIRES", 86400))
    cache.set(f"owned_task:{kind}:{task.id}",
              {"user_id": user.pk, "tenant_id": getattr(user, "client_id", None)}, timeout=timeout)
    return task


def require_task_owner(user, task_id, kind):
    expected = {"user_id": user.pk, "tenant_id": getattr(user, "client_id", None)}
    if not getattr(user, "is_active", False) or cache.get(f"owned_task:{kind}:{task_id}") != expected:
        raise Http404
