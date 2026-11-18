"""Stored outputs for explicit document operations, with current actor permissions."""
import mimetypes
import os
import shutil
import uuid
import zipfile

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from core import access
from core.models import File, Run, Storage
from document_operations.models import FileAuditLog, FileFolderLink, FileVersion


def current_actor(actor):
    from rest_framework.exceptions import PermissionDenied
    actor = get_user_model().objects.get(pk=actor.pk, is_active=True)
    if access.user_role_names(actor) == {"Guest"}:
        raise PermissionDenied("This account cannot change documents.")
    return actor


def output_path(actor, folder, filename):
    name = access.safe_filename(filename)
    directory = os.path.join(settings.MEDIA_ROOT, "uploads", str(access.tenant_id(actor) or "personal"), str(actor.pk),
                             access.safe_filename(folder.project_id), access.safe_filename(folder.service_id),
                             timezone.now().strftime("%Y%m%d"), "operations", uuid.uuid4().hex)
    os.makedirs(directory, exist_ok=False)
    return os.path.join(directory, name)


def register_output(path, actor, folder, *, filename=None, origin=None):
    """Copies/archives are separate outputs even when their bytes match an upload."""
    run = Run.objects.create(user=actor, status="Completed")
    storage = Storage.objects.create(user=actor, content_type=ContentType.objects.get_for_model(run),
                                     object_id=run.pk, output_storage_location=path, upload_storage_location=path)
    document = File.objects.create(run=run, storage=storage, user=actor, filename=filename or os.path.basename(path),
                                   filepath=path, file_size=os.path.getsize(path),
                                   file_type=mimetypes.guess_type(path)[0] or "application/octet-stream",
                                   extension=os.path.splitext(path)[1].lstrip(".").lower()[:10], md5_hash=None,
                                   project_id=folder.project_id, service_id=folder.service_id, status="Completed",
                                   origin_file=origin, content=origin.content if origin else None,
                                   document_type=origin.document_type if origin else None)
    return FileFolderLink.objects.create(file=document, folder=folder)


def output_result(link, message):
    return {"message": message, "file_id": link.file_id, "file_link_id": link.pk,
            "filename": link.file.filename, "download_url": f"/api/v1/core/download/?file_id={link.file_id}"}


def copy_document(file_id, folder_id, actor, *, action="download"):
    from core.processing import source_version
    actor = current_actor(actor)
    source = access.get_file_for_user(actor, file_id, action)
    folder = access.get_folder_for_user(actor, folder_id, "manage")
    if not os.path.isfile(source.filepath):
        raise FileNotFoundError("The source document is missing from storage.")
    snapshot = source_version(source)
    stem, ext = os.path.splitext(source.filename)
    name = f"{stem}_copy{ext}"
    target = output_path(actor, folder, name)
    try:
        shutil.copy2(source.filepath, target)
        actor = current_actor(actor)
        current = access.get_file_for_user(actor, file_id, action)
        folder = access.get_folder_for_user(actor, folder_id, "manage")
        if source_version(current) != snapshot:
            raise ValueError("The source document changed while it was being copied. Try again.")
        with transaction.atomic():
            link = register_output(target, actor, folder, filename=name, origin=source)
            FileAuditLog.objects.create(file=link.file, user=actor, action="created", notes="Copied document",
                                        extra={"source_file_id": source.pk})
        return link
    except Exception:
        if os.path.isfile(target):
            os.remove(target)
        raise


def zip_documents(link_ids, actor):
    from core.processing import source_version
    actor = current_actor(actor)
    links = access.get_file_links_for_user(actor, link_ids, "zip")
    if not links:
        raise ValueError("Choose at least one document.")
    # Keep the archive in the requester's own tree, never in a shared owner's folder.
    from document_operations.utils import get_or_create_folder_tree
    folder = get_or_create_folder_tree([timezone.now().strftime("%Y%m%d"), "archives"], user=actor,
                                       project_id=links[0].file.project_id, service_id=links[0].file.service_id)
    path = output_path(actor, folder, "documents.zip")
    try:
        used = set()
        snapshots = {link.file_id: source_version(link.file) for link in links}
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for link in links:
                if not os.path.isfile(link.file.filepath):
                    raise FileNotFoundError("A selected document is missing from storage.")
                name = access.safe_filename(link.file.filename)
                stem, ext = os.path.splitext(name)
                suffix = 0
                while name in used:
                    suffix += 1
                    name = f"{stem}_{link.pk}{'_' + str(suffix) if suffix > 1 else ''}{ext}"
                used.add(name)
                archive.write(link.file.filepath, arcname=name)
        actor = current_actor(actor)
        current = access.get_file_links_for_user(actor, link_ids, "zip")
        if any(source_version(link.file) != snapshots[link.file_id] for link in current):
            raise ValueError("A selected document changed while the archive was created. Try again.")
        with transaction.atomic():
            result = register_output(path, actor, folder)
        return output_result(result, "Archive created.")
    except Exception:
        if os.path.isfile(path):
            os.remove(path)
        raise


def rename_document(file_id, new_name, actor, expected_updated_at=None):
    document = access.get_file_for_user(actor, file_id, "rename")
    if expected_updated_at is not None and document.updated_at.isoformat() != expected_updated_at:
        raise ValueError("The document changed after rename was requested. Try again.")
    name = access.safe_filename(new_name)
    path = document.filepath
    if not os.path.isfile(path):
        raise FileNotFoundError("The source document is missing from storage.")
    # Legacy copies can share a path. Allocate private bytes before renaming so
    # renaming one row can never break or overwrite a different document.
    target = os.path.join(os.path.dirname(path), name)
    if os.path.abspath(target) == os.path.abspath(path):
        document.filename = name
        document.save(update_fields=["filename", "updated_at"])
        return {"file_id": document.pk, "message": "File renamed."}
    if os.path.exists(target) or File.objects.exclude(pk=document.pk).filter(filepath=path).exists():
        link = document.folder_links.select_related("folder").first()
        if not link:
            raise ValueError("The document has no storage folder.")
        target = output_path(document.user, link.folder, name)
        shutil.copy2(path, target)
    else:
        os.rename(path, target)
    document.filename = name
    document.filepath = target
    document.extension = os.path.splitext(name)[1].lstrip(".").lower()[:10]
    document.save(update_fields=["filename", "filepath", "extension", "updated_at"])
    if document.storage_id:
        Storage.objects.filter(pk=document.storage_id).update(upload_storage_location=target, output_storage_location=target)
    FileAuditLog.objects.create(file=document, user=actor, action="renamed", extra={"new_name": name})
    return {"message": "File renamed.", "file_id": document.pk, "filename": name}


def restore_version(link_id, version_number, actor):
    actor = current_actor(actor)
    link = access.get_file_link_for_user(actor, link_id, "restore")
    with transaction.atomic():
        document = File.objects.select_for_update().get(pk=link.file_id)
        from core.processing import source_version
        snapshot = source_version(document)
        version = document.versions.get(version_number=version_number)
        if not os.path.isfile(version.file_path) or not os.path.isfile(document.filepath):
            raise FileNotFoundError("The requested version or current document is missing from storage.")
        number = (document.versions.aggregate(last=Max("version_number"))["last"] or 0) + 1
        directory = os.path.join(settings.MEDIA_ROOT, "versions", str(document.pk))
        os.makedirs(directory, exist_ok=True)
        backup = os.path.join(directory, f"v{number}-{uuid.uuid4().hex}")
        shutil.copy2(document.filepath, backup)
        FileVersion.objects.create(file=document, version_number=number, file_path=backup, uploaded_by=actor)
        target = output_path(document.user, link.folder, document.filename)
        shutil.copy2(version.file_path, target)
        try:
            actor = current_actor(actor)
            access.get_file_link_for_user(actor, link_id, "restore")
            if source_version(File.objects.get(pk=document.pk)) != snapshot:
                raise ValueError("The current document changed during version restore. Try again.")
        except Exception:
            os.remove(target)
            raise
        document.filepath = target
        document.file_size = os.path.getsize(target)
        document.md5_hash = None  # restoring identical bytes must not collide with a deliberate copy
        document.content = None  # extraction/indexing must not keep the replaced version's text
        document.save(update_fields=["filepath", "file_size", "md5_hash", "content", "updated_at"])
        if document.storage_id:
            Storage.objects.filter(pk=document.storage_id).update(upload_storage_location=target, output_storage_location=target)
        FileAuditLog.objects.create(file=document, user=actor, action="version_restored", extra={"version": version_number})
    return {"message": f"Restored version {version_number}.", "file_id": document.pk, "backup_version": number}
