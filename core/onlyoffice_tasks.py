# core/onlyoffice_tasks.py
from __future__ import annotations

import os
import logging
import requests

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404

from core.models import File
from core.onlyoffice_utils import file_md5

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=10, name="core.onlyoffice.fetch_and_save")
def onlyoffice_fetch_and_save(self, file_id: int, file_url: str) -> dict:
    """
    Download the edited document from OnlyOffice Document Server and
    atomically overwrite the existing file on disk. Then update DB metadata.

    Args:
        file_id: your File.id being edited
        file_url: DS temporary URL (or any URL) to fetch the latest binary

    Returns:
        Minimal dict describing the saved file.
    """
    f = get_object_or_404(File, id=file_id)

    # ---- network setup (bypass env proxies) ----
    sess = requests.Session()
    sess.trust_env = False
    T_CONN = int(settings.ONLYOFFICE.get("HTTP_CONNECT_TIMEOUT", 5))
    T_READ = int(settings.ONLYOFFICE.get("HTTP_READ_TIMEOUT", 240))

    try:
        r = sess.get(file_url, stream=True, timeout=(T_CONN, max(120, T_READ)))
        r.raise_for_status()
    except requests.RequestException as e:
        logger.exception("OnlyOffice fetch failed for file_id=%s url=%s", file_id, file_url)
        # retry transient errors
        try:
            self.retry(exc=e)
        except self.MaxRetriesExceededError:
            raise

    # ---- write to a temp file, then atomic replace ----
    final_path = f.filepath
    tmp_path = final_path + ".tmp"
    os.makedirs(os.path.dirname(final_path), exist_ok=True)

    with open(tmp_path, "wb") as out:
        for chunk in r.iter_content(1024 * 1024):
            if chunk:
                out.write(chunk)

    os.replace(tmp_path, final_path)

    # ---- update DB metadata ----
    new_size = os.path.getsize(final_path)
    new_md5 = file_md5(final_path)

    with transaction.atomic():
        f.file_size = new_size
        # keep same MIME/extension — still a DOCX for normal edits
        f.md5_hash = new_md5
        f.status = "Saved"
        # Saved bytes invalidate old OCR/search/assistant text. Otherwise a successful
        # edit could keep answering from the previous document indefinitely.
        f.content = ''
        f.save(update_fields=["file_size", "md5_hash", "status", "content", "updated_at"])

    logger.info("OnlyOffice saved file_id=%s size=%s md5=%s", f.id, new_size, new_md5)
    return {"ok": True, "file_id": f.id, "size": new_size, "md5": new_md5}

