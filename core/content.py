"""
Replace a stored document's bytes safely (Word add-in save, future editors).

Order of checks: edit access, legal hold, check-out by someone else. The current bytes are kept as a version
(DocumentVersion) before the new bytes replace them atomically (temp file + os.replace), so nothing is lost and
a failed write never leaves a half-written document. Saved bytes invalidate extracted text, so search, OCR and
the assistant never answer from the previous content.
"""
from __future__ import annotations

import hashlib
import os

from django.db import transaction
from rest_framework.exceptions import PermissionDenied

MAX_BYTES = 100 * 1024 * 1024


def replace_document_content(file_obj, data: bytes, actor, note: str = ""):
    from core.access import can_access_file
    from core.checkout import assert_not_locked
    from core.holds import assert_not_held
    from core.models import File
    from document_versioning.models import DocumentVersion
    from document_versioning.utils import create_version_snapshot, get_next_version_number

    if not can_access_file(actor, file_obj, "edit"):
        raise PermissionDenied("You can view but not edit this document.")
    assert_not_held(file_obj, "edit")
    assert_not_locked(actor, file_obj, "edit")
    from core.frozen import assert_not_frozen
    assert_not_frozen(file_obj)
    if not data or len(data) > MAX_BYTES:
        raise ValueError("The document is empty or larger than 100 MB.")
    path = file_obj.filepath
    if not os.path.isfile(path):
        raise ValueError("The stored document is missing.")

    with transaction.atomic():
        current = File.objects.select_for_update().get(pk=file_obj.pk)
        number = get_next_version_number(current)
        snapshot = create_version_snapshot(current, path, number, actor.email)
        with open(snapshot, "rb") as fh:
            old_md5 = hashlib.md5(fh.read()).hexdigest()
        DocumentVersion.objects.create(file=current, version_number=number, filepath=snapshot, file_hash=old_md5,
                                       file_size=os.path.getsize(snapshot), created_by=actor.email,
                                       change_summary=(note or "Saved from an Office add-in")[:1000])
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        new_md5 = hashlib.md5(data).hexdigest()
        duplicate = File.objects.filter(user_id=current.user_id, md5_hash=new_md5).exclude(pk=current.pk).exists()
        current.file_size = len(data)
        current.md5_hash = None if duplicate else new_md5
        current.status = "Saved"
        current.content = ""
        current.save(update_fields=["file_size", "md5_hash", "status", "content", "updated_at"])
    return current, number
