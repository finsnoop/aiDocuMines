"""GET/PUT /api/v1/home-analytics/dashboard-layout/?project_id=&service_id= : per-user dashboard layout and UI preferences."""
import json

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import DashboardLayout

MAX_BYTES = 256 * 1024  # generous for widget layouts, small enough to stop abuse


def _scope(request):
    return {
        "project_id": str(request.query_params.get("project_id") or "")[:255],
        "service_id": str(request.query_params.get("service_id") or "")[:255],
    }


class DashboardLayoutView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        row = DashboardLayout.objects.filter(user=request.user, **_scope(request)).first()
        if row is None:
            return Response({"layout": None, "ui_prefs": {}, "updated_at": None})
        return Response({"layout": row.layout, "ui_prefs": row.ui_prefs, "updated_at": row.updated_at})

    def put(self, request):
        has_layout, has_prefs = "layout" in request.data, "ui_prefs" in request.data
        if not (has_layout or has_prefs):
            return Response({"error": "Send 'layout' (a list) and/or 'ui_prefs' (an object)."},
                            status=status.HTTP_400_BAD_REQUEST)
        layout, prefs = request.data.get("layout"), request.data.get("ui_prefs")
        if has_layout and not isinstance(layout, list):
            return Response({"error": "'layout' must be a list."}, status=status.HTTP_400_BAD_REQUEST)
        if has_prefs and not isinstance(prefs, dict):
            return Response({"error": "'ui_prefs' must be an object."}, status=status.HTTP_400_BAD_REQUEST)
        if len(json.dumps({"l": layout, "p": prefs})) > MAX_BYTES:
            return Response({"error": "Layout is too large."}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        row, _ = DashboardLayout.objects.get_or_create(user=request.user, **_scope(request))
        if has_layout:
            row.layout = layout
        if has_prefs:
            row.ui_prefs = {**(row.ui_prefs or {}), **prefs}
        row.save()
        return Response({"ok": True, "updated_at": row.updated_at})

    post = put
