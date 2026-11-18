"""Shared, source-versioned text preparation for actual downstream consumers."""
import hashlib
import json
import logging
import os
import uuid

from django.core.cache import cache

LOGGER = logging.getLogger(__name__)
LOCK_SECONDS = 20 * 60
ATTEMPT_SECONDS = 24 * 60 * 60


class TextPreparationBusy(RuntimeError):
    """A consumer should retry; expensive text preparation is already in progress."""


def _fingerprint(file):
    try:
        stat = os.stat(file.filepath)
        physical = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        physical = None
    # Status/label saves change updated_at without changing source bytes. They
    # must not create a second OCR lock for the same uploaded document.
    source = (file.pk, file.filepath, file.md5_hash, physical)
    return hashlib.sha256(json.dumps(source, default=str).encode()).hexdigest()


def shared_file_text(file, *, allow_ocr=False):
    """Reuse extracted content; permit PDF-only OCR when a consumer needs text.

    Callers enforce their own file ACL before entering this internal helper.
    Cache locks deduplicate ES/Milvus/Assistant work across worker processes;
    extracted text persists in File.content, without another post-save pipeline.
    """
    from core.models import File
    from document_search.utils import extract_text

    current = File.objects.filter(pk=file.pk).only("content", "filepath", "md5_hash", "updated_at").first()
    if current is None or current.filepath != file.filepath or current.md5_hash != file.md5_hash:
        return ""
    if (current.content or "").strip():
        file.content = current.content
        return current.content
    version = _fingerprint(current)
    key = f"text-preparation:{version}"
    lock = key + ":lock"
    token = uuid.uuid4().hex
    try:
        acquired = cache.add(lock, token, timeout=LOCK_SECONDS)
    except Exception as exc:
        if allow_ocr:
            raise TextPreparationBusy("Text preparation is temporarily unavailable; retry shortly.") from exc
        return extract_text(current.filepath) if current.filepath else ""
    if not acquired:
        text = File.objects.filter(pk=current.pk, md5_hash=current.md5_hash, filepath=current.filepath).values_list("content", flat=True).first()
        if text:
            return text
        raise TextPreparationBusy("Text is being prepared by another request; retry shortly.")
    try:
        # Another worker may have finished between the first read and our lock.
        text = File.objects.filter(pk=current.pk, md5_hash=current.md5_hash, filepath=current.filepath).values_list("content", flat=True).first()
        if (text or "").strip():
            file.content = text
            return text
        text = extract_text(current.filepath) if current.filepath else ""
        method = "native"
        if not text.strip() and allow_ocr and str(current.filepath).lower().endswith(".pdf"):
            if cache.get(key + ":ocr-attempt"):
                return ""
            # Recorded before the expensive call: failed or blank scans cannot
            # generate an unlimited series of OCR attempts from parallel jobs.
            cache.set(key + ":ocr-attempt", {"method": "ocr", "source": version}, timeout=ATTEMPT_SECONDS)
            text = extract_text(current.filepath, allow_ocr=True)
            method = "ocr"
        text = (text or "").replace("\x00", "").strip()
        if _fingerprint(current) != version:
            return ""  # source bytes changed while extraction was in progress
        if text:
            updated = File.objects.filter(pk=current.pk, filepath=current.filepath,
                                          md5_hash=current.md5_hash).update(content=text)
            if not updated:
                return ""  # an editor save must never regain stale extracted text
            file.content = text
        cache.set(key + ":provenance", {"method": method, "chars": len(text), "source": version,
                                       "downstream_ocr": method == "ocr"}, timeout=ATTEMPT_SECONDS)
        return text
    finally:
        try:
            if cache.get(lock) == token:
                cache.delete(lock)
        except Exception:
            LOGGER.debug("Text preparation lock will expire naturally.")
