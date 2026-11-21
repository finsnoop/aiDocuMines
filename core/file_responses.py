"""
Send stored documents to the browser safely.

A browser shows only a few types itself. Anything else it is told to display "inline" is
downloaded anyway (Word, Excel, archives), and HTML/SVG/XML would be *run* on the API's own
origin. So:

* previews: only INLINE_PREVIEW_TYPES, always with a fixed content type (never guessed from
  the name, so a file renamed to .pdf cannot be served as HTML);
* downloads: inline only for those same types; everything else is an attachment.

Keep INLINE_PREVIEW_TYPES in step with aiDocuMines-Frontend/src/app/documents/viewer-kind.ts.
"""
import mimetypes
import os

from django.http import FileResponse, JsonResponse

INLINE_PREVIEW_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".txt": "text/plain; charset=utf-8",
}


def can_preview_inline(path) -> bool:
    return os.path.splitext(str(path or ""))[1].lower() in INLINE_PREVIEW_TYPES


def _harden(response):
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


def preview_response(path, filename=None):
    """Inline response for a previewable file, or 415 for every other type."""
    ext = os.path.splitext(str(path))[1].lower()
    if ext not in INLINE_PREVIEW_TYPES:
        return JsonResponse({"error": "This file type cannot be previewed in the browser. Open it in the "
                                      "document viewer or download it."}, status=415)
    return _harden(FileResponse(open(path, "rb"), as_attachment=False,
                                filename=filename or os.path.basename(path),
                                content_type=INLINE_PREVIEW_TYPES[ext]))


def download_response(path, filename=None, inline=False, watermark=None):
    """Download; shown inline only when asked for AND the type is safe to display.

    `watermark`: text to stamp on a PDF copy (core/watermark.py); the stored file is not changed.
    """
    name = filename or os.path.basename(path)
    if watermark:
        from django.http import HttpResponse
        from core.watermark import watermarked_or_none
        stamped = watermarked_or_none(path, watermark)
        if stamped is not None:
            response = HttpResponse(stamped, content_type="application/pdf")
            disposition = "inline" if inline else "attachment"
            from django.utils.http import content_disposition_header
            response["Content-Disposition"] = content_disposition_header(disposition == "attachment", name)
            return _harden(response)
    if inline and can_preview_inline(path):
        return preview_response(path, name)
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return _harden(FileResponse(open(path, "rb"), as_attachment=True, filename=name, content_type=content_type))
