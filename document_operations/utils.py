import os
import shutil
import zipfile
from django.conf import settings
from core.models import File
from .models import Folder, FileFolderLink, FileVersion, FileAuditLog
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
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


# Folder Operations
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
    restore_folder_recursive(folder)
    return {"message": f"Restored folder {folder.name} (recursive)"}


def delete_folder(folder_id, actor=None):
    from core.access import can_access_file
    folder = Folder.objects.get(pk=folder_id)
    children = list(folder.subfolders.values_list("pk", flat=True))
    for child in children:
        delete_folder(child, actor=actor)
    for link in list(folder.files.select_related("file")):
        if actor is not None and not can_access_file(actor, link.file, "delete"):
            link.delete()  # remove this folder's location, never delete someone else's document
        else:
            delete_file(link.pk, actor=actor)
    name = folder.name
    folder.delete()
    return {"message": f"Deleted folder {name}."}


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


# File Operations
def rename_file(file_id, new_name, actor=None):
    from core.access import safe_filename
    new_name = safe_filename(new_name)  # a name, never a path
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
    link = FileFolderLink.objects.select_related("file").get(pk=file_link_id)
    document = link.file
    path = document.filepath
    link.delete()
    # Removing one location must not remove other links, or legacy documents
    # intentionally reusing the same stored bytes.
    if document.folder_links.exists():
        return {"message": "Document removed from this folder."}
    document.delete()
    if path and not File.objects.filter(filepath=path).exists() and os.path.isfile(path):
        os.remove(path)
    return {"message": "Document deleted."}


def copy_file(file_id, destination_folder_id, actor=None):
    from document_operations.actions import copy_document
    source = File.objects.get(pk=file_id)
    return copy_document(file_id, destination_folder_id, actor or source.user)


def duplicate_file(file_link_id, actor=None):
    link = FileFolderLink.objects.select_related("file").get(pk=file_link_id)
    from document_operations.actions import copy_document
    return copy_document(link.file_id, link.folder_id, actor or link.file.user, action="duplicate")


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
    # A hint is not a password. Retain the legacy call signature so queued jobs
    # fail clearly without modifying existing protection or claiming success.
    return {"error": "Set a password again from the document menu; a password hint alone cannot protect a document."}


def same_tenant(user_a, user_b) -> bool:
    if not user_a or not user_b:
        return False
    client_a = getattr(user_a, "client_id", None)
    client_b = getattr(user_b, "client_id", None)
    return client_a is not None and client_a == client_b


def _entry_allows(entry, level, now):
    if entry.expires_at and entry.expires_at < now:
        return False
    if level == "read":
        return entry.can_read
    if level == "write":
        return entry.can_write
    if level == "delete":
        return entry.can_delete
    if level in ("share", "owner"):
        return entry.can_share
    return False


def get_user_accessible_file_ids(user, min_level="read"):
    owned = list(File.objects.filter(user=user).values_list("id", flat=True))
    client_id = getattr(user, "client_id", None)
    if not client_id:
        return owned

    now = timezone.now()
    entries = FileAccessEntry.objects.filter(
        user=user,
        file_link__file__user__client_id=client_id,
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
    if min_level == "write":
        entries = entries.filter(can_write=True)
    elif min_level == "delete":
        entries = entries.filter(can_delete=True)
    elif min_level in ("owner", "share"):
        entries = entries.filter(can_share=True)
    else:
        entries = entries.filter(can_read=True)
    shared = list(entries.values_list("file_link__file_id", flat=True))
    if min_level == "read":
        shared.extend(
            FileFolderLink.objects.filter(
                shared_with=user,
                file__user__client_id=client_id,
            ).values_list("file_id", flat=True)
        )
    return list(set(owned).union(shared))


def has_file_access(user, file: File, level="read") -> bool:
    from core.access import can_access_file
    action = {"read": "view", "write": "rename", "delete": "delete", "share": "share", "owner": "share"}.get(level)
    return bool(action and can_access_file(user, file, action))


def get_file_access_audit(file: File):
    file_link = getattr(file, "folder_link", None)
    if not file_link:
        return []

    return FileAccessEntry.objects.filter(file_link=file_link).select_related("user", "granted_by")





def user_has_access(user, file_id, level="read") -> bool:
    try:
        file = File.objects.get(id=file_id)
    except File.DoesNotExist:
        return False
    return has_file_access(user, file, level)



# Bulk Operations
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


# File Versioning
def create_file_version(file: File, uploaded_by=None):
    from django.db.models import Max
    latest = file.versions.aggregate(last=Max("version_number"))["last"] or 0
    number = latest + 1
    path = os.path.join(settings.MEDIA_ROOT, "versions", str(file.pk), f"v{number}-{uuid.uuid4().hex}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    shutil.copy2(file.filepath, path)
    return FileVersion.objects.create(file=file, version_number=number, file_path=path, uploaded_by=uploaded_by)


# Sharing
def generate_share_token():
    return secrets.token_urlsafe(32)


def generate_public_link(file_link: FileFolderLink, actor=None):
    if not file_link.is_public or not file_link.public_token:
        file_link.public_token = generate_share_token()
        file_link.shared_link_created_at = timezone.now()
        file_link.is_public = True
        file_link.save()

        if actor:
            FileAuditLog.objects.create(
                file=file_link.file,
                user=actor,
                action="public_link",
                extra={"file_link_id": file_link.id}
            )

    return file_link.public_token


def revoke_public_link(file_link: FileFolderLink):
    file_link.public_token = None
    file_link.is_public = False
    file_link.shared_link_created_at = None
    file_link.save()


# Logging
def log_file_activity(file: File, user: User, action: str, notes=None):
    FileAuditLog.objects.create(file=file, user=user, action=action, notes=notes)


# Create nested folders based on a path list
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


# Link a File to its folder (used after file creation)
def link_file_to_folder(file, folder):
    """
    One-to-one link between File and Folder.
    """
    return FileFolderLink.objects.get_or_create(file=file, folder=folder)


def update_access_level_for_user(file_link, user_id, access_level, granted_by):
    perms = {
        "read":  {"can_read": True, "can_write": False, "can_share": False, "can_delete": False},
        "write": {"can_read": True, "can_write": True,  "can_share": False, "can_delete": False},
        "owner": {"can_read": True, "can_write": True,  "can_share": True, "can_delete": True},
    }.get(access_level, {"can_read": True})

    user = get_object_or_404(User, pk=user_id)
    owner = file_link.file.user
    if not same_tenant(owner, user) or not same_tenant(owner, granted_by):
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("Documents can only be shared with users in the same organisation.")

    FileAccessEntry.objects.update_or_create(
        file_link=file_link,
        user=user,
        defaults={**perms, "granted_by": granted_by}
    )

    # Audit log for permission update
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
    return {"success": False, **protect_file_with_password(file_id, password_hint)}


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
    except Exception:
        # Fallback: derive folder parts from uploads-relative path
        # filepath = .../uploads/{client}/{user}/{project}/{service}/{date}/{file}
        relative_path = file_obj.filepath.split("uploads/", 1)[-1]
        parts = relative_path.split("/")
        # Skip client, user, project, service → keep only date-level folders
        folder_parts = parts[4:-1]
        if folder_parts:
            folder = get_or_create_folder_tree(
                folder_parts, user=file_obj.user,
                project_id=file_obj.project_id, service_id=file_obj.service_id
            )
            FileFolderLink.objects.create(file=file_obj, folder=folder)


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

