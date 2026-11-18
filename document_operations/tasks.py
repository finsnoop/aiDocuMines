# tasks.py
from celery import shared_task
from core.models import File
from .models import Folder, FileVersion, FileAuditLog, FileFolderLink
from django.contrib.auth import get_user_model
from django.db import models
from .utils import (
    move_files_to_trash,
    reassign_folder_for_files,
    rename_file,
    rename_folder,
    delete_file,
    delete_folder,
    duplicate_file,
    copy_file,
    zip_files,
    protect_file_with_password,
    restore_file_from_trash,
    restore_folder_from_trash,
    generate_public_link,
    revoke_public_link,
    has_file_access,
    same_tenant,
    trash_folder_recursive,
    restore_folder_recursive,
)

import os
from .models import FileAccessEntry
from django.contrib.auth.models import Group
from django.utils import timezone
from .models import FileFolderLink

from celery import shared_task
import secrets

User = get_user_model()

from core import access
from django.db import transaction
from rest_framework.exceptions import PermissionDenied


def _operation_actor(user_id, file_obj=None, owner=None):
    actor = User.objects.get(pk=user_id) if user_id is not None else (owner or file_obj.user)
    if not actor.is_active or access.user_role_names(actor) == {"Guest"}:
        raise PermissionDenied("This account cannot change documents.")
    return actor


def log_permission_denied(actor, obj_type, obj_id, action):
    print(f"PERMISSION DENIED: User {actor.id} tried to {action} {obj_type} {obj_id} at {timezone.now()}")

@shared_task
def async_bulk_trash_files(file_ids, actor_user_id=None):
    actor = _operation_actor(actor_user_id, FileFolderLink.objects.get(pk=file_ids[0]).file)
    links = access.get_file_links_for_user(actor, file_ids, "delete")
    count = move_files_to_trash([link.pk for link in links], actor=actor)
    return {"message": f"Moved {count} file(s) to trash.", "file_ids": file_ids}

@shared_task
def async_bulk_move_files_to_folder(file_ids, folder_id, actor_user_id=None):
    actor = _operation_actor(actor_user_id, FileFolderLink.objects.get(pk=file_ids[0]).file)
    links = access.get_file_links_for_user(actor, file_ids, "move")
    folder = access.get_folder_for_user(actor, folder_id, "manage")
    with transaction.atomic():
        for link in links:
            file = link.file
            if file.user_id != actor.pk and not access.is_tenant_admin(actor) and not access.is_platform_superuser(actor) and not access.can_access_file(actor, file, "share"):
                raise ValueError("Copy this shared document instead; moving it requires its owner's sharing authority.")
            other_projects = file.folder_links.exclude(pk=link.pk).exclude(folder__project_id=folder.project_id)
            if other_projects.exists():
                raise ValueError("This document has another folder link in a different matter; copy it instead.")
            link.folder = folder
            link.save(update_fields=["folder"])
            file.project_id, file.service_id = folder.project_id, folder.service_id
            file.save(update_fields=["project_id", "service_id", "updated_at"])
            FileAuditLog.objects.create(file=file, user=actor, action="updated", notes="Moved document", extra={"folder_id": str(folder.pk)})
    return {"message": "Documents moved.", "file_ids": file_ids, "folder_id": str(folder.pk)}

@shared_task
def async_rename_file(file_id, new_name, actor_user_id=None, expected_updated_at=None):
    from document_operations.actions import rename_document
    actor = _operation_actor(actor_user_id, File.objects.get(pk=file_id))
    return rename_document(file_id, new_name, actor, expected_updated_at=expected_updated_at)

@shared_task
def async_rename_folder(folder_id, new_name, actor_user_id=None):
    folder = Folder.objects.get(pk=folder_id)
    actor = _operation_actor(actor_user_id, owner=folder.user)
    folder = access.get_folder_for_user(actor, folder_id, "manage")
    rename_folder(folder, access.safe_filename(new_name))
    return {"message": "Folder renamed.", "folder_id": str(folder.pk)}

@shared_task
def async_delete_file(file_link_id, actor_user_id=None):
    link = FileFolderLink.objects.get(id=file_link_id)
    actor = link.file.user if actor_user_id is None else User.objects.get(pk=actor_user_id)

    if not has_file_access(actor, link.file, level="write"):
        return {"error": "Permission denied."}

    FileAuditLog.objects.create(file=link.file, user=actor, action="deleted")
    return delete_file(file_link_id)

@shared_task
def async_delete_folder(folder_id):
    return delete_folder(folder_id)

@shared_task
def async_duplicate_file(file_link_id, actor_user_id=None):
    from document_operations.actions import copy_document, output_result
    link = FileFolderLink.objects.select_related("file").get(pk=file_link_id)
    actor = _operation_actor(actor_user_id, link.file)
    access.get_file_link_for_user(actor, file_link_id, "duplicate")
    result = copy_document(link.file_id, link.folder_id, actor, action="duplicate")
    return output_result(result, "Document duplicated.")

@shared_task
def async_copy_file(file_id, destination_folder_id, actor_user_id=None):
    from document_operations.actions import copy_document, output_result
    actor = _operation_actor(actor_user_id, File.objects.get(pk=file_id))
    return output_result(copy_document(file_id, destination_folder_id, actor), "Document copied.")

@shared_task
def async_zip_files(file_ids, actor_user_id=None):
    from document_operations.actions import zip_documents
    actor = _operation_actor(actor_user_id, FileFolderLink.objects.get(pk=file_ids[0]).file)
    return zip_documents(file_ids, actor)

@shared_task
def async_password_protect_file(file_link_id, password_hint, actor_user_id=None):
    """Legacy hint-only jobs cannot securely set a password; ask for resubmission."""
    link = FileFolderLink.objects.select_related("file").get(pk=file_link_id)
    actor = _operation_actor(actor_user_id, link.file)
    access.get_file_link_for_user(actor, file_link_id, "protect")
    return protect_file_with_password(file_link_id, password_hint)

@shared_task
def async_restore_file(file_link_id, actor_user_id=None):
    link = FileFolderLink.objects.get(id=file_link_id)
    actor = link.file.user if actor_user_id is None else User.objects.get(pk=actor_user_id)

    result = restore_file_from_trash(file_link_id)
    FileAuditLog.objects.create(file=link.file, user=actor, action="restored")
    return result

@shared_task
def async_restore_folder(folder_id):
    return restore_folder_from_trash(folder_id)

@shared_task
def async_create_file_version(file_id, new_path, user_id):
    file = File.objects.get(pk=file_id)
    user = User.objects.get(pk=user_id)

    latest_version = file.versions.aggregate(max_version=models.Max('version_number'))['max_version'] or 0
    FileVersion.objects.create(
        file=file,
        version_number=latest_version + 1,
        file_path=new_path,
        uploaded_by=user
    )
    FileAuditLog.objects.create(file=file, user=user, action="updated", notes=f"Version {latest_version + 1} saved")
    return {"message": f"Version {latest_version + 1} created", "file_id": file_id}

# Async share/unshare

@shared_task
def async_share_file_with_users(file_link_id, user_ids, actor_user_id):
    link = FileFolderLink.objects.get(id=file_link_id)
    actor = User.objects.get(pk=actor_user_id)

    if not has_file_access(actor, link.file, level="share"):
        log_permission_denied(actor, "File", file_link_id, "share")
        return {"error": "Permission denied."}

    added = []
    skipped = []
    for uid in user_ids:
        user = User.objects.get(id=uid)
        if not same_tenant(link.file.user, user) or not same_tenant(actor, link.file.user):
            skipped.append(uid)
            continue
        link.shared_with.add(user)
        added.append(uid)

    link.is_shared = True
    link.save()
    FileAuditLog.objects.create(file=link.file, user=actor, action="shared", notes=f"Shared with {added}")
    return {"message": f"Shared file with users {added}", "skipped_other_tenant": skipped}

@shared_task
def async_unshare_file_with_users(file_link_id, user_ids, actor_user_id):
    link = FileFolderLink.objects.get(id=file_link_id)
    actor = User.objects.get(pk=actor_user_id)

    if not has_file_access(actor, link.file, level="share"):
        log_permission_denied(actor, "File", file_link_id, "unshare")
        return {"error": "Permission denied."}

    removed = []
    for uid in user_ids:
        user = User.objects.get(id=uid)
        link.shared_with.remove(user)
        removed.append(uid)

    if link.shared_with.count() == 0:
        link.is_shared = False
    link.save()
    FileAuditLog.objects.create(file=link.file, user=actor, action="updated", notes=f"Unshared with {removed}")
    return {"message": f"Unshared file from users {removed}"}

# Async public link actions

@shared_task
def async_generate_public_link_task(file_link_id, actor_user_id):
    link = FileFolderLink.objects.get(id=file_link_id)
    actor = User.objects.get(pk=actor_user_id)

    if not has_file_access(actor, link.file, level="share"):
        log_permission_denied(actor, "File", file_link_id, "generate public link")
        return {"error": "Permission denied."}

    token = generate_public_link(link)
    FileAuditLog.objects.create(file=link.file, user=actor, action="shared", notes=f"Generated public link")
    return {"public_token": token}

@shared_task
def async_revoke_public_link_task(file_link_id, actor_user_id):
    link = FileFolderLink.objects.get(id=file_link_id)
    actor = User.objects.get(pk=actor_user_id)

    if not has_file_access(actor, link.file, level="share"):
        log_permission_denied(actor, "File", file_link_id, "revoke public link")
        return {"error": "Permission denied."}

    revoke_public_link(link)
    FileAuditLog.objects.create(file=link.file, user=actor, action="updated", notes="Revoked public link")
    return {"message": "Public link revoked"}

# Folder Audit Logs

@shared_task
def async_log_folder_action(folder_id, user_id, action, notes=None):
    try:
        folder = Folder.objects.get(id=folder_id)
        user = User.objects.get(id=user_id)
    except (Folder.DoesNotExist, User.DoesNotExist):
        return {"error": "Invalid folder or user"}

    FileAuditLog.objects.create(
        file=None,
        user=user,
        action=action,
        notes=f"[FOLDER {folder.name}] {notes or ''}"
    )
    return {"message": f"Audit log for folder {folder.name} recorded"}

@shared_task
def async_share_file_with_group(file_id, group_id, access_rights, expires_at=None):
    try:
        file = File.objects.get(pk=file_id)
        group = Group.objects.get(pk=group_id)
    except (File.DoesNotExist, Group.DoesNotExist):
        return {"error": "Invalid file or group ID."}

    entry, _ = FileAccessEntry.objects.update_or_create(
        file=file,
        group=group,
        defaults={
            "can_read": access_rights.get("can_read", True),
            "can_write": access_rights.get("can_write", False),
            "can_delete": access_rights.get("can_delete", False),
            "can_share": access_rights.get("can_share", False),
            "expires_at": expires_at,
        }
    )

    return {"message": f"File shared with group {group.name}", "file_id": file_id}

@shared_task
def async_set_file_access_with_expiry(file_id, user_id, access_rights, expires_at=None):
    try:
        file = File.objects.get(pk=file_id)
        user = User.objects.get(pk=user_id)
    except (File.DoesNotExist, User.DoesNotExist):
        return {"error": "Invalid file or user ID."}

    entry, _ = FileAccessEntry.objects.update_or_create(
        file=file,
        user=user,
        defaults={
            "can_read": access_rights.get("can_read", True),
            "can_write": access_rights.get("can_write", False),
            "can_delete": access_rights.get("can_delete", False),
            "can_share": access_rights.get("can_share", False),
            "expires_at": expires_at,
        }
    )

    return {"message": f"Access set for {user.email}", "file_id": file_id}

@shared_task
def async_check_expired_file_accesses():
    now = timezone.now()
    expired = FileAccessEntry.objects.filter(expires_at__lt=now)
    count = expired.count()
    expired.delete()
    return {"message": f"Expired access entries deleted", "count": count}

@shared_task
def async_grant_public_link(file_link_id):
    try:
        link = FileFolderLink.objects.get(id=file_link_id)
        link.is_public = True
        if not link.public_token:
            link.public_token = secrets.token_urlsafe(16)
        link.save()
    except FileFolderLink.DoesNotExist:
        pass

@shared_task
def async_bulk_trash_folders(folder_ids, actor_user_id=None):
    actor = _operation_actor(actor_user_id, owner=Folder.objects.get(pk=folder_ids[0]).user)
    folders = [access.get_folder_for_user(actor, fid, "manage") for fid in folder_ids]
    for folder in folders:
        trash_folder_recursive(folder)
    return {"message": f"Trashed {len(folders)} folder(s) recursively"}

@shared_task
def async_bulk_restore_folders(folder_ids):
    for fid in folder_ids:
        try:
            folder = Folder.objects.get(id=fid)
            restore_folder_recursive(folder)
        except Folder.DoesNotExist:
            continue
    return {"message": f"Restored {len(folder_ids)} folder(s) recursively"}

@shared_task
def async_bulk_delete_folders(folder_ids, actor_user_id=None):
    actor = _operation_actor(actor_user_id, owner=Folder.objects.get(pk=folder_ids[0]).user)
    folders = [access.get_folder_for_user(actor, fid, "manage") for fid in folder_ids]
    for folder in folders:
        delete_folder(folder.pk, actor=actor)
    return {"message": f"Deleted {len(folders)} folder(s)."}


def _collect_paths(folder, paths_list):
    """Recursively collect file paths from a folder and its subfolders."""
    for link in folder.files.select_related('file').all():
        if link.file.filepath:
            paths_list.append(link.file.filepath)
    for child in folder.subfolders.all():
        _collect_paths(child, paths_list)
