"""
The one way documents enter aiDocuMines: into a matter, exactly as Matters -> New matter -> Upload does.

Every source uses it (the Matters page, Nextcloud, Google Drive and the other connectors, Zapier / Make uploads),
so storage, the sidebar's matter / service / date hierarchy, duplicate handling, upload policy and the processing
chain (text extraction, indexing, classification) are the same whatever the origin. Anonymisation is requested separately.

    stored at  MEDIA_ROOT/uploads/<organisation>/<user>/<matter name>/matter/<YYYYMMDD>/<HHMMSS>_<filename>
    recorded   File(project_id=<matter name>, service_id="matter"), linked into the folder tree
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import shutil
from datetime import datetime

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError

from core.access import tenant_id
from core.models import File, Run, Storage

logger = logging.getLogger(__name__)


class MatterBatch:
    """One arrival of documents into a matter (one Run, one timestamp, one storage folder)."""

    def __init__(self, user, matter, source: str = "upload"):
        self.user, self.matter, self.source = user, matter, source
        self.stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        self.base_dir = os.path.join(settings.MEDIA_ROOT, "uploads", str(tenant_id(user) or "personal"), str(user.id),
                                     matter.name, "matter", self.stamp[:8])
        os.makedirs(self.base_dir, exist_ok=True)
        self.run = Run.objects.create(user=user, status="Pending")

    def temp_dir(self) -> str:
        path = os.path.join(self.base_dir, "temp")
        os.makedirs(path, exist_ok=True)
        return path

    def add(self, temp_path: str, filename: str, size: int, md5: str, file_type: str | None = None) -> dict:
        """Store one file already written to `temp_path`. Returns {filename, status, file_id?, message}.
        status: uploaded | added (same content already stored by this user) | duplicate | rejected."""
        from custom_authentication.policies import upload_violation
        from document_operations.utils import register_file_folder_link

        user, matter = self.user, self.matter
        entry = {"filename": filename}
        reason = upload_violation(user, filename, size)
        if reason or not size:
            _discard(temp_path)
            entry.update(status="rejected", message=reason or "This file is empty.")
            return entry

        existing = File.objects.filter(user=user, md5_hash=md5).first()
        if existing:
            _discard(temp_path)
            if existing.project_id == matter.name:
                entry.update(status="duplicate", file_id=existing.id, message="Already in this matter.")
                return entry
            copy = File.objects.create(
                run=self.run, storage=existing.storage, filename=existing.filename, filepath=existing.filepath,
                file_size=existing.file_size, file_type=existing.file_type, md5_hash=None,
                user=user, project_id=matter.name, service_id="matter", origin_file=existing,
                extension=existing.extension, content=existing.content, status=existing.status,
            )
            register_file_folder_link(copy)
            entry.update(status="added", file_id=copy.id, message="Added from your existing documents.")
            return entry

        final_path = os.path.join(self.base_dir, f"{self.stamp[8:]}_{filename}")
        if os.path.exists(final_path):
            stem, ext = os.path.splitext(filename)
            final_path = os.path.join(self.base_dir, f"{self.stamp[8:]}_{stem}-{md5[:6]}{ext}")
        shutil.move(temp_path, final_path)
        storage = Storage.objects.create(user=user, content_type=ContentType.objects.get_for_model(self.run),
                                         object_id=self.run.run_id, upload_storage_location=final_path)
        ext = os.path.splitext(filename)[1].lstrip(".").lower()[:10] or "unknown"
        try:
            fresh = File.objects.create(
                run=self.run, storage=storage, filename=filename, filepath=final_path, file_size=size,
                file_type=file_type or mimetypes.guess_type(filename)[0] or "application/octet-stream",
                md5_hash=md5, user=user, project_id=matter.name, service_id="matter", extension=ext,
            )
        except IntegrityError:
            entry.update(status="duplicate", message="Already uploaded.")
            return entry
        register_file_folder_link(fresh)
        try:
            from core.views import queue_post_upload_processing
            queue_post_upload_processing(user, self.run, fresh, matter.name, "matter")
        except Exception:  # processing is best-effort; the document is stored either way
            logger.exception("Post-upload processing could not be queued for file %s", fresh.id)
        entry.update(status="uploaded", file_id=fresh.id, message="Uploaded.")
        return entry

    def add_snapshot(self, source: File, service_id: str = "benchmark") -> tuple[File, str]:
        """A frozen copy of `source` as it is now: its own bytes (never shared with the original, so later edits to
        the original cannot change it), linked into this matter's folder tree. Returns (copy, sha256 of the bytes).
        md5_hash stays empty so the copy never collides with the original's duplicate check."""
        from document_operations.utils import register_file_folder_link
        if not source.filepath or not os.path.isfile(source.filepath):
            raise ValueError("The document's contents are missing from storage.")
        final_path = os.path.join(self.base_dir, f"{self.stamp[8:]}_{os.urandom(3).hex()}_{source.filename}")
        sha = hashlib.sha256()
        with open(source.filepath, "rb") as src, open(final_path, "wb") as dst:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                sha.update(chunk)
                dst.write(chunk)
        storage = Storage.objects.create(user=self.user, content_type=ContentType.objects.get_for_model(self.run),
                                         object_id=self.run.run_id, upload_storage_location=final_path)
        copy = File.objects.create(
            run=self.run, storage=storage, filename=source.filename, filepath=final_path, file_size=os.path.getsize(final_path),
            file_type=source.file_type, md5_hash=None, user=self.user, project_id=self.matter.name, service_id=service_id,
            origin_file=source, extension=source.extension, content=source.content, status=source.status,
        )
        register_file_folder_link(copy)
        return copy, sha.hexdigest()

    def add_stream(self, filename: str, chunks) -> dict:
        """Write a download (an iterator of bytes) to a temp file, hashing as it goes, then add it."""
        from core.access import safe_filename
        try:
            filename = safe_filename(filename)
        except ValueError:
            filename = "document"
        temp = os.path.join(self.temp_dir(), f"{os.urandom(6).hex()}_{filename}")
        md5, size = hashlib.md5(), 0
        try:
            with open(temp, "wb") as fh:
                for c in chunks:
                    md5.update(c)
                    size += len(c)
                    fh.write(c)
        except Exception:
            _discard(temp)
            raise
        return self.add(temp, filename, size, md5.hexdigest())

    def finish(self):
        """Tidy the temp folder and mark the matter as active (as the Matters upload does)."""
        try:
            os.rmdir(os.path.join(self.base_dir, "temp"))
        except OSError:
            pass
        try:
            from core.views_matters import _touch
            _touch(self.matter)
        except Exception:
            logger.exception("Could not update matter %s activity", self.matter.id)


def _discard(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def matter_for_ingest(user, matter_id):
    """A matter the user may add documents to, or a ValueError with a message for the user."""
    from django.http import Http404
    from core.views_matters import _get_matter
    from core.utils import is_safe_path_segment
    if not matter_id:
        raise ValueError("Choose the matter the documents belong to.")
    try:
        matter = _get_matter(user, matter_id)
    except Http404:
        raise ValueError("That matter was not found, or you are not on its team.")
    if matter.status == "closed":
        raise ValueError("This matter is closed. Reopen it to add documents.")
    if not is_safe_path_segment(matter.name):
        raise ValueError("Rename this matter before adding documents (slashes are not allowed).")
    return matter
