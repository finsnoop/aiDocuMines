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


def marked_bytes(path, name, watermark, owner_client_id):
    """Stamped bytes, None (no mark needed or possible and allowed), or a 403 response when the organisation
    blocks copies that cannot carry a watermark."""
    from core.watermark import WatermarkRefused, watermarked_or_none
    try:
        return watermarked_or_none(path, watermark, owner_client_id=owner_client_id, name=name)
    except WatermarkRefused as exc:
        return JsonResponse({"error": str(exc)}, status=403)


def stamped_response(data, name, inline=False):
    """Serve stamped bytes: inline only for types the browser displays safely (fixed content type)."""
    from django.http import HttpResponse
    from django.utils.http import content_disposition_header
    ext = os.path.splitext(str(name))[1].lower()
    show_inline = inline and ext in INLINE_PREVIEW_TYPES
    content_type = INLINE_PREVIEW_TYPES.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"
    response = HttpResponse(data, content_type=content_type)
    response["Content-Disposition"] = content_disposition_header(not show_inline, name)
    return _harden(response)


def download_response(path, filename=None, inline=False, watermark=None, owner_client_id=None):
    """Download; shown inline only when asked for AND the type is safe to display.

    `watermark`: text to stamp on this copy (core/watermark.py: PDF, Office, images); the stored file is not
    changed. `owner_client_id`: the document owner's organisation, whose policy decides what happens to formats
    that cannot carry a mark (403 when it blocks them).
    """
    name = filename or os.path.basename(path)
    if watermark:
        stamped = marked_bytes(path, name, watermark, owner_client_id)
        if isinstance(stamped, JsonResponse):
            return stamped
        if stamped is not None:
            return stamped_response(stamped, name, inline=inline)
    if inline and can_preview_inline(path):
        return preview_response(path, name)
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return _harden(FileResponse(open(path, "rb"), as_attachment=True, filename=name, content_type=content_type))
