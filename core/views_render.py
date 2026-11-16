"""Endpoints that let the document viewer show any file (see core/document_render.py)."""
import os

from django.http import FileResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import get_file_for_user
from custom_authentication.permissions import IsClientOrAdminOrSuperUser

MISSING = "This file is listed, but its contents are missing from storage. Upload it again to view it."


def _stored_path(file_obj):
    for candidate in (file_obj.filepath, (file_obj.filepath or "").replace("/app/", "", 1)):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


class FileRenderView(APIView):
    """GET /api/v1/core/files/<id>/render/ -> how to show the file.

    JSON {kind: pdf|image|media|text|html|table|slides|email|archive|details, ...}. For pdf, image and
    media the bytes come from FileRenderContentView.
    """
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def get(self, request, file_id):
        file_obj = get_file_for_user(request.user, file_id, "view")
        path = _stored_path(file_obj)
        if not path:
            return Response({"error": MISSING}, status=404)
        from core.document_render import render_manifest
        try:
            manifest = render_manifest(file_obj, path)
        except OSError:
            return Response({"error": "The file could not be read."}, status=500)
        resp = Response(manifest)
        resp["Cache-Control"] = "private, no-store"
        return resp


class FileRenderContentView(APIView):
    """GET /api/v1/core/files/<id>/render/content/ -> the displayable bytes (original or PDF/PNG rendition)."""
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def get(self, request, file_id):
        file_obj = get_file_for_user(request.user, file_id, "view")
        path = _stored_path(file_obj)
        if not path:
            return Response({"error": MISSING}, status=404)
        from core.document_render import render_content
        found = render_content(file_obj, path)
        if not found:
            return Response({"error": "No displayable version of this file is available."}, status=415)
        out, content_type = found
        name = os.path.splitext(file_obj.filename or "document")[0] + os.path.splitext(out)[1]
        resp = FileResponse(open(out, "rb"), as_attachment=False, content_type=content_type, filename=name)
        resp["X-Content-Type-Options"] = "nosniff"
        resp["Cache-Control"] = "private, no-store"
        resp["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
        return resp
