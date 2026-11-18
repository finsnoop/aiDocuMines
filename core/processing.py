"""Authorisation and source guards for queued document processors."""
import os

from django.contrib.auth import get_user_model
from rest_framework.exceptions import PermissionDenied

from core.access import get_file_for_user, user_has_perm


def processing_file(file_id, user_id=None, *, run=None, permission="document.update"):
    """Reload permissions in the worker, including after an expensive operation.

    Older queued tasks had no actor argument. Only an owner's matching legacy
    run may use that compatibility path; new submissions always pass user_id.
    """
    from core.models import File
    source = File.objects.select_related("user").get(pk=file_id)
    if user_id is None:
        owner = source.user
        if run is None or run.client_id != owner.client_id or run.client_name not in {owner.username, owner.email}:
            raise PermissionDenied("This job has no authorised requesting user. Submit it again.")
        user_id = owner.pk
    actor = get_user_model().objects.get(pk=user_id, is_active=True)
    if not user_has_perm(actor, permission):
        raise PermissionDenied("Your role cannot run document processing.")
    source = get_file_for_user(actor, file_id, "process")
    if run is not None and run.client_id != actor.client_id:
        raise PermissionDenied("The job belongs to a different organisation.")
    return actor, source


def source_version(file):
    stat = os.stat(file.filepath)
    return (file.filepath, file.md5_hash, stat.st_size, stat.st_mtime_ns,
            file.user_id, file.project_id, file.service_id)


def unchanged_source(file_id, user_id, version, *, run=None, permission="document.update"):
    actor, source = processing_file(file_id, user_id, run=run, permission=permission)
    if source_version(source) != version:
        raise ValueError("The source document changed during processing. Submit it again.")
    return actor, source
