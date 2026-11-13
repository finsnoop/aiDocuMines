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
from document_operations.utils import set_password_protection
import secrets

User = get_user_model()

def log_permission_denied(actor, obj_type, obj_id, action):
    print(f"🚨 PERMISSION DENIED: User {actor.id} tried to {action} {obj_type} {obj_id} at {timezone.now()}")

@shared_task
def async_bulk_trash_files(file_ids):
    updated_count = move_files_to_trash(file_ids)
    return {
        "message": f"Moved {updated_count} file(s) to trash.",
        "file_ids": file_ids,
    }

@shared_task
def async_bulk_move_files_to_folder(file_ids, folder_id):
    try:
        folder = Folder.objects.get(id=folder_id)
    except Folder.DoesNotExist:
        return {"error": "Target folder does not exist."}

    links = reassign_folder_for_files(file_ids, folder)
    return {
        "message": f"Moved {len(links)} file(s) to folder '{folder.name}'.",
        "folder_id": folder_id,
        "file_ids": file_ids,
    }

@shared_task
def async_rename_file(file_id, new_name, actor_user_id=None):
    try:
        file = File.objects.get(id=file_id)
    except File.DoesNotExist:
        return {"error": f"File with ID {file_id} does not exist."}

    actor = file.user if actor_user_id is None else User.objects.get(pk=actor_user_id)
    if not has_file_access(actor, file, level="write"):
        return {"error": "Permission denied."}

    old_path = file.filepath
    new_path = os.path.join(os.path.dirname(old_path), new_name)

    if os.path.exists(old_path):
        os.rename(old_path, new_path)

    file.filename = new_name
    file.filepath = new_path
    file.save()

    FileAuditLog.objects.create(file=file, user=actor, action="renamed", notes=f"Renamed to {new_name}")
    return {"message": f"File renamed to {new_name}", "file_id": file.id}

@shared_task
def async_rename_folder(folder_id, new_name):
    folder = Folder.objects.get(id=folder_id)
    rename_folder(folder, new_name)
    return {"message": f"Folder renamed to {new_name}", "folder_id": folder_id}

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
    link = FileFolderLink.objects.get(id=file_link_id)
    actor = link.file.user if actor_user_id is None else User.objects.get(pk=actor_user_id)

    if not has_file_access(actor, link.file, level="write"):
        return {"error": "Permission denied."}

    new_link = duplicate_file(file_link_id)
    FileAuditLog.objects.create(file=new_link.file, user=actor, action="created", notes="Duplicated")
    return {"message": f"Duplicated file {link.file.filename}", "new_file_id": new_link.file.id}

@shared_task
def async_copy_file(file_id, destination_folder_id, actor_user_id=None):
    file = File.objects.get(id=file_id)
    actor = file.user if actor_user_id is None else User.objects.get(pk=actor_user_id)

    if not has_file_access(actor, file, level="read"):
        return {"error": "Permission denied."}

    new_link = copy_file(file_id, destination_folder_id)
    FileAuditLog.objects.create(file=new_link.file, user=actor, action="created", notes="Copied to another folder")
    return {"message": f"Copied file to folder {new_link.folder.name}", "file_id": new_link.file.id}

@shared_task
def async_zip_files(file_ids):
    return zip_files(file_ids)

@shared_task
def async_password_protect_file(file_link_id, password_hint, actor_user_id=None):
    link = FileFolderLink.objects.get(id=file_link_id)
    actor = link.file.user if actor_user_id is None else User.objects.get(pk=actor_user_id)

    if not has_file_access(actor, link.file, level="write"):
        return {"error": "Permission denied."}

    result = protect_file_with_password(file_link_id, password_hint)
    FileAuditLog.objects.create(file=link.file, user=actor, action="updated", notes="Password protected")
    return result

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

# 🔁 Async share/unshare

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

# 🔗 Async public link actions

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

# 🗂️ Folder Audit Logs

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

@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def async_password_protect_file(self, file_id, password_hint):
    result = set_password_protection(file_id, password_hint)
    if not result["success"]:
        raise Exception(result["message"])
    return result

@shared_task
def async_bulk_trash_folders(folder_ids):
    for fid in folder_ids:
        try:
            folder = Folder.objects.get(id=fid)
            trash_folder_recursive(folder)
        except Folder.DoesNotExist:
            continue
    return {"message": f"Trashed {len(folder_ids)} folder(s) recursively"}

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
def async_bulk_delete_folders(folder_ids):
    import os
    from core.models import File as CoreFile

    all_filepaths = []
    for fid in folder_ids:
        try:
            folder = Folder.objects.get(id=fid)
            _collect_paths(folder, all_filepaths)
            folder.delete()
        except Folder.DoesNotExist:
            continue

    # Remove physical files from disk after DB deletion
    for fpath in all_filepaths:
        if fpath and os.path.isfile(fpath):
            try:
                os.remove(fpath)
            except OSError:
                pass

    return {"message": f"Deleted {len(folder_ids)} folder(s) permanently"}


def _collect_paths(folder, paths_list):
    """Recursively collect file paths from a folder and its subfolders."""
    for link in folder.files.select_related('file').all():
        if link.file.filepath:
            paths_list.append(link.file.filepath)
    for child in folder.subfolders.all():
        _collect_paths(child, paths_list)
