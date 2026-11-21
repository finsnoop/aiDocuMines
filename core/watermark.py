"""
Watermarks on downloaded and shared PDFs, when the document owner's organisation turns them on
(Users & access, Settings: watermark_downloads). The stored document is never changed: each copy is
stamped as it is served, so a leaked copy shows who took it and when.

Template placeholders: {email} (the signed-in viewer, or "Shared link"), {date}, {org}.
"""
from __future__ import annotations

import logging
import os

from django.utils import timezone

logger = logging.getLogger(__name__)
DEFAULT_TEMPLATE = "Confidential · {email} · {date}"
MAX_PAGES = 2000


def text_for(owner_client_id, viewer=None) -> str | None:
    """The watermark for this copy, or None when the owner's organisation does not watermark."""
    if not owner_client_id:
        return None
    from custom_authentication.models import OrganisationPolicy
    policy = OrganisationPolicy.objects.filter(client_id=owner_client_id).select_related("client") \
        .only("watermark_downloads", "watermark_text", "client__name").first()
    if not policy or not policy.watermark_downloads:
        return None
    template = (policy.watermark_text or DEFAULT_TEMPLATE)[:200]
    values = {"email": getattr(viewer, "email", None) or "Shared link",
              "date": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
              "org": getattr(policy.client, "name", "")}
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return DEFAULT_TEMPLATE.format(**values)


def is_pdf(path) -> bool:
    return str(path).lower().endswith(".pdf")


def stamp_pdf(path, text) -> bytes:
    """A watermarked copy of the PDF at `path` (diagonal mark plus a footer line on every page)."""
    import fitz
    with fitz.open(path) as doc:
        if doc.page_count > MAX_PAGES:
            raise ValueError("Too many pages to watermark.")
        for page in doc:
            rect = page.rect
            size = max(18, min(48, rect.width / max(len(text), 1) * 1.6))
            centre = fitz.Point(rect.width / 2, rect.height / 2)
            start = fitz.Point(centre.x - len(text) * size * 0.25, centre.y)
            page.insert_text(start, text, fontsize=size, color=(0.55, 0.55, 0.55), fill_opacity=0.22,
                             morph=(centre, fitz.Matrix(-35)), overlay=True)
            page.insert_text(fitz.Point(24, rect.height - 14), text, fontsize=8, color=(0.4, 0.4, 0.4),
                             fill_opacity=0.8, overlay=True)
        return doc.tobytes(garbage=3, deflate=True)


def watermarked_or_none(path, text):
    """Bytes of a stamped copy, or None when nothing should (or can) be stamped."""
    if not text or not is_pdf(path) or not os.path.isfile(path):
        return None
    try:
        return stamp_pdf(path, text)
    except Exception:
        # Never serve an unmarked copy when a watermark is required.
        logger.exception("Could not watermark %s", path)
        raise
