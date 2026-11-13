import os
import shutil
import zipfile
from django.conf import settings
from core.models import File
from .models import Folder, FileFolderLink, FileVersion, FileAuditLog
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.db import transaction
import uuid
import secrets
from .models import FileAccessEntry
from django.shortcuts import get_object_or_404
from .models import FileAccessEntry
from document_operations.models import FileFolderLink, Folder
from document_operations.models import Folder, FileFolderLink

import logging
from document_operations.models import FileFolderLink, FileAuditLog

logger = logging.getLogger(__name__)

User = get_user_model()


def get_folder_upload_path(user_id, client_id, project_id, service_id, run_id, subfolder=None):
    base_path = os.path.join(settings.MEDIA_ROOT, "uploads", str(user_id), client_id, project_id, service_id, str(run_id))
    return os.path.join(base_path, subfolder) if subfolder else base_path


# 📁 Folder Operations
def rename_folder(folder: Folder, new_name: str):
    folder.name = new_name
    folder.save()
    return folder


def trash_folder(folder: Folder):
    folder.is_trashed = True
    folder.save()
    for file in folder.files.all():
        file.is_trashed = True
        file.save()


def restore_folder_from_trash(folder_id):
    folder = Folder.objects.get(id=folder_id)
    folder.is_trashed = False
    folder.save()
    for link in folder.files.all():
        link.is_trashed = False
        link.save()
    return {"message": f"Restored folder {folder.name}"}


def delete_folder(folder: Folder):
    folder.delete()


def trash_folder_recursive(folder):
    folder.is_trashed = True
    folder.save()
    for link in folder.files.all():
        link.is_trashed = True
        link.save()
    for sub in folder.subfolders.all():
        trash_folder_recursive(sub)


def restore_folder_recursive(folder):
    folder.is_trashed = False
    folder.save()
    for link in folder.files.all():
        link.is_trashed = False
        link.save()
    for sub in folder.subfolders.all():
        restore_folder_recursive(sub)


# 📄 File Operations
def rename_file(file_id, new_name, actor=None):
    file = File.objects.get(id=file_id)
    old_path = file.filepath
    dir_path = os.path.dirname(old_path)
    new_path = os.path.join(dir_path, new_name)

    if os.path.exists(old_path):
        os.rename(old_path, new_path)

    file.filename = new_name
    file.filepath = new_path
    file.save()

    if actor:
        FileAuditLog.objects.create(
            file=file,
            user=actor,
            action="renamed",
            extra={"new_name": new_name}
        )


    return {"message": f"File renamed to {new_name}", "file_id": file.id}


def move_file_to_folder(file_link: FileFolderLink, new_folder: Folder):
    file_link.folder = new_folder
    file_link.save()
    return file_link


def restore_file_from_trash(file_link_id, actor=None):
    link = FileFolderLink.objects.get(id=file_link_id)
    link.is_trashed = False
    link.save()

    if actor:
        FileAuditLog.objects.create(
            file=link.file,
            user=actor,
            action="restored",
            extra={"file_link_id": link.id}
        )

    return {"message": f"Restored file {link.file.filename}"}


def delete_file(file_link_id, actor=None):
    link = FileFolderLink.objects.get(id=file_link_id)
    file = link.file

    # Log deletion before actual delete
    if actor:
        FileAuditLog.objects.create(
            file=file,
            user=actor,
            action="deleted",
            extra={"file_link_id": file_link_id}
        )

    file.delete()
    link.delete()

    return {"message": f"Deleted file {file_link_id}"}


def copy_file(file_id, destination_folder_id):
    file = File.objects.get(id=file_id)
    folder = Folder.objects.get(id=destination_folder_id)
    new_file = File.objects.create(
        filename=f"{file.filename}_copy",
        filepath=file.filepath,
        file_size=file.file_size,
        file_type=file.file_type,
        md5_hash=None,
        run=file.run,
        user=file.user,
        project_id=file.project_id,
        service_id=file.service_id,
        status="Pending",
    )
    return FileFolderLink.objects.create(file=new_file, folder=folder)


def duplicate_file(file_link_id):
    link = FileFolderLink.objects.get(id=file_link_id)
    return copy_file(link.file.id, link.folder.id)


def zip_files(file_link_ids):
    zip_name = f"archive_{uuid.uuid4()}.zip"
    target_dir = os.path.join(settings.MEDIA_ROOT, "zips")
    os.makedirs(target_dir, exist_ok=True)
    zip_path = os.path.join(target_dir, zip_name)
    with zipfile.ZipFile(zip_path, 'w') as zf:
        for link_id in file_link_ids:
            link = FileFolderLink.objects.get(id=link_id)
            zf.write(link.file.filepath, arcname=link.file.filename)
    return {"zip_path": zip_path, "zip_name": zip_name}


def protect_file_with_password(file_link_id, password_hint):
    link = FileFolderLink.objects.get(id=file_link_id)
    link.password_protected = True
    link.password_hint = password_hint
    link.save()
    return {"message": f"File {link.file.filename} password protected."}


def get_user_accessible_file_ids(user, min_level="read"):
    """
    Returns a list of file IDs the user has access to based on access level.
    """
    owned = File.objects.filter(user=user).values_list("id", flat=True)

    access_entries = FileAccessEntry.objects.filter(user=user)
    if min_level == "write":
        access_entries = access_entries.filter(access_level__in=["write", "owner"])
    elif min_level == "owner":
        access_entries = access_entries.filter(access_level="owner")

    shared = access_entries.select_related("file_link__file").values_list("file_link__file__id", flat=True)

    return list(set(owned).union(shared))


def has_file_access(user, file: File, level="read") -> bool:
    if file.user == user:
        return True

    file_link = getattr(file, "folder_link", None)
    if not file_link:
        return False

    try:
        entry = FileAccessEntry.objects.get(file_link=file_link, user=user)
        if entry.expires_at and entry.expires_at < timezone.now():
            return False
        if level == "read":
            return entry.can_read
        if level == "write":
            return entry.can_write
        if level == "delete":
            return entry.can_delete
        if level == "share":
            return entry.can_share
        return False
    except FileAccessEntry.DoesNotExist:
        return False

'''
def has_file_access(user, file: File, level="read") -> bool:
    if file.user == user:
        return True
    try:
        entry = FileAccessEntry.objects.get(file_link=file.folder_link, user=user)
        if level == "read":
            return entry.access_level in ["read", "write", "owner"]
        if level == "write":
            return entry.access_level in ["write", "owner"]
        if level == "owner":
            return entry.access_level == "owner"
        return False
    except FileAccessEntry.DoesNotExist:
        return False
'''

def get_file_access_audit(file: File):
    file_link = getattr(file, "folder_link", None)
    if not file_link:
        return []

    return FileAccessEntry.objects.filter(file_link=file_link).select_related("user", "granted_by")


'''
def get_user_accessible_file_ids(user):
    owned = File.objects.filter(user=user).values_list("id", flat=True)
    shared = FileFolderLink.objects.filter(shared_with=user).values_list("file_id", flat=True)
    return list(set(owned).union(shared))
'''


'''
def user_has_access(user, file_id, level='read') -> bool:
    file = File.objects.filter(id=file_id, user=user).exists()
    shared = FileFolderLink.objects.filter(file_id=file_id, shared_with=user).exists()
    return file or shared
'''

def user_has_access(user, file_id, level="read") -> bool:
    try:
        file = File.objects.get(id=file_id)
    except File.DoesNotExist:
        return False

    if file.user == user:
        return True

    file_link = getattr(file, "folder_link", None)
    if not file_link:
        return False

    try:
        entry = FileAccessEntry.objects.get(file_link=file_link, user=user)
        if entry.expires_at and entry.expires_at < timezone.now():
            return False
        if level == "read":
            return entry.can_read
        if level == "write":
            return entry.can_write
        if level == "delete":
            return entry.can_delete
        if level == "share":
            return entry.can_share
        return False
    except FileAccessEntry.DoesNotExist:
        return False


'''
def user_has_access(user, file_id, level="read") -> bool:
    try:
        file = File.objects.get(id=file_id)
    except File.DoesNotExist:
        return False

    # Owner (uploader) has full access
    if file.user == user:
        return True

    # Access entries define granular permissions
    file_link = getattr(file, "folder_link", None)
    if not file_link:
        return False

    try:
        entry = FileAccessEntry.objects.get(file_link=file_link, user=user)

        if entry.expires_at and entry.expires_at < timezone.now():
            return False

        if level == "read":
            return entry.access_level in ["read", "write", "owner"]
        if level == "write":
            return entry.access_level in ["write", "owner"]
        if level == "owner":
            return entry.access_level == "owner"
        return False
    except FileAccessEntry.DoesNotExist:
        return False
'''

# 🔄 Bulk Operations
def move_files_to_trash(file_link_ids, actor=None):
    count = 0
    for link_id in file_link_ids:
        link = FileFolderLink.objects.filter(id=link_id).first()
        if link:
            link.is_trashed = True
            link.save()
            count += 1

            if actor:
                FileAuditLog.objects.create(
                    file=link.file,
                    user=actor,
                    action="trashed",
                    extra={"file_link_id": link.id}
                )
    return count


def reassign_folder_for_files(file_link_ids, new_folder):
    updated_links = []
    with transaction.atomic():
        for link_id in file_link_ids:
            link = FileFolderLink.objects.filter(id=link_id).first()
            if link:
                link.folder = new_folder
                link.save()
                updated_links.append(link)
    return updated_links


# 📜 File Versioning
def create_file_version(file: File, uploaded_by=None):
    latest_version = file.versions.first()
    new_version = (latest_version.version_number + 1) if latest_version else 1

    version_path = os.path.join(settings.MEDIA_ROOT, "versions", str(file.id), f"v{new_version}")
    os.makedirs(os.path.dirname(version_path), exist_ok=True)
    shutil.copy(file.file.path, version_path)

    return FileVersion.objects.create(
        file=file,
        version_number=new_version,
        file_path=version_path,
        uploaded_by=uploaded_by
    )


# 🔗 Sharing
def generate_share_token():
    return secrets.token_urlsafe(32)


def generate_public_link(file_link: FileFolderLink, actor=None):
    if not file_link.public_token:
        file_link.public_token = generate_share_token()
        file_link.shared_link_created_at = timezone.now()
        file_link.is_public = True
        file_link.save()

        if actor:
            FileAuditLog.objects.create(
                file=file_link.file,
                user=actor,
                action="public_link",
                extra={"file_link_id": file_link.id, "public_token": file_link.public_token}
            )

    return file_link.public_token


def revoke_public_link(file_link: FileFolderLink):
    file_link.public_token = None
    file_link.is_public = False
    file_link.shared_link_created_at = None
    file_link.save()


# 🕵️ Logging
def log_file_activity(file: File, user: User, action: str, notes=None):
    FileAuditLog.objects.create(file=file, user=user, action=action, notes=notes)


# 📁📁 Create nested folders based on a path list
def get_or_create_folder_tree(path_parts, *, user, project_id, service_id):
    """
    Ensure each part in `path_parts` exists as a Folder, nested parent->child.
    Returns the final (leaf) Folder object.
    """
    parent = None
    for name in path_parts:
        folder, _ = Folder.objects.get_or_create(
            name=name,
            parent=parent,
            user=user,
            project_id=project_id,
            service_id=service_id,
            defaults={"is_trashed": False}
        )
        parent = folder
    return parent


# 🔗 Link a File to its folder (used after file creation)
def link_file_to_folder(file, folder):
    """
    One-to-one link between File and Folder.
    """
    return FileFolderLink.objects.get_or_create(file=file, folder=folder)


def update_access_level_for_user(file_link, user_id, access_level, granted_by):
    perms = {
        "read":  {"can_read": True, "can_write": False, "can_share": False, "is_owner": False},
        "write": {"can_read": True, "can_write": True,  "can_share": False, "is_owner": False},
        "owner": {"can_read": True, "can_write": True,  "can_share": True,  "is_owner": True},
    }.get(access_level, {"can_read": True})

    user = get_object_or_404(User, pk=user_id)

    FileAccessEntry.objects.update_or_create(
        file_link=file_link,
        user=user,
        defaults={**perms, "granted_by": granted_by}
    )

    # ✅ Audit log for permission update
    FileAuditLog.objects.create(
        file=file_link.file,
        user=granted_by,
        action="access_updated",
        extra={
            "target_user": user.email,
            "new_access_level": access_level,
            "permissions": perms
        }
    )

    return {
        "message": f"Access level updated to {access_level}",
        "user": user.email,
        "level": access_level
    }



def set_password_protection(file_id, password_hint, actor=None):
    """
    Set password protection metadata on a file.
    
    Args:
        file_id (int): ID of the file to protect
        password_hint (str): The hint to show to the user
        actor (User or None): User who triggered the protection (optional)
        
    Returns:
        dict: result with status and message
    """
    try:
        file_link = FileFolderLink.objects.get(file_id=file_id)

        file_link.password_hint = password_hint
        file_link.password_protected = True
        file_link.save()

        # Optional audit log entry
        if actor:
            FileAuditLog.objects.create(
                file=file_link.file,
                user=actor,
                action="password_set",
                extra={"password_hint": password_hint}
            )
        return {
            "success": True,
            "message": f"Password protection applied to file_id={file_id}."
        }

    except FileFolderLink.DoesNotExist:
        logger.error(f"[Password Protect] FileFolderLink not found for file_id={file_id}")
        return {
            "success": False,
            "message": f"FileFolderLink does not exist for file_id={file_id}."
        }
    except Exception as e:
        logger.exception(f"[Password Protect] Unexpected error for file_id={file_id}: {e}")
        return {
            "success": False,
            "message": f"Unexpected error: {str(e)}"
        }




# Folder names that only exist in server storage paths and must never appear in a user's tree.
STORAGE_PATH_NAMES = {"app", "media", "uploads", "demo", "temp", "tmp", "var", "home", "srv", "data", ""}


def folder_parts_for(file_obj):
    """
    Folders below <matter>/<service>/ for a file, as the user should see them:
    the sub-path after "<project_id>/<service_id>/" in its storage path, or, when
    the storage path does not contain it (copies of another matter's file,
    renamed matters, imported files), the upload date (YYYYMMDD).
    Server storage directories (app/media/uploads/<ids>/...) are never used.
    """
    from django.utils import timezone

    path = (file_obj.filepath or "").replace("\\", "/")
    marker = f"{file_obj.project_id}/{file_obj.service_id}/"
    parts = []
    if file_obj.project_id and file_obj.service_id and marker in path:
        parts = [p for p in os.path.dirname(path.split(marker, 1)[1]).split("/") if p]
        parts = [p for p in parts if p.lower() != "temp"]
    if not parts:
        created = getattr(file_obj, "created_at", None) or timezone.now()
        parts = [created.strftime("%Y%m%d")]
    return parts


def register_file_folder_link(file_obj):
    """Link a file to its folder (<matter>/<service>/<date or sub-folders>), once."""
    if file_obj.folder_links.exists():
        return  # already linked
    try:
        folder = get_or_create_folder_tree(
            folder_parts_for(file_obj), user=file_obj.user,
            project_id=file_obj.project_id, service_id=file_obj.service_id,
        )
        FileFolderLink.objects.create(file=file_obj, folder=folder)
    except Exception as e:
        raise Exception(f"Failed to register FileFolderLink for file {file_obj.id}: {e}")


def repair_misplaced_folder_links(batch_size=500):
    """
    Re-file documents that were linked under server storage folders
    (app/media/uploads/...) and delete those folders once empty.
    Returns (files_moved, folders_deleted). Safe to run repeatedly.
    """
    roots = list(Folder.objects.filter(parent__isnull=True, name__in=STORAGE_PATH_NAMES)
                 .values_list("id", flat=True))
    if not roots:
        return 0, 0
    bad, frontier = set(roots), list(roots)
    while frontier:
        children = list(Folder.objects.filter(parent_id__in=frontier).values_list("id", flat=True))
        frontier = [c for c in children if c not in bad]
        bad.update(frontier)

    moved = 0
    links = FileFolderLink.objects.filter(folder_id__in=bad).select_related("file", "file__user")
    for link in links.iterator(chunk_size=batch_size):
        f = link.file
        target = get_or_create_folder_tree(folder_parts_for(f), user=f.user,
                                           project_id=f.project_id, service_id=f.service_id)
        if target.id != link.folder_id:
            FileFolderLink.objects.filter(pk=link.pk).update(folder=target)
            moved += 1

    deleted = 0
    for folder_id in roots:  # delete the storage-path chains that are now empty
        ids = [fid for fid in bad if fid]
        if not FileFolderLink.objects.filter(folder_id__in=ids).exists():
            deleted += Folder.objects.filter(id=folder_id).delete()[0]
    return moved, deleted

