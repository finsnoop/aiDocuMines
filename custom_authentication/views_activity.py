"""The signed-in user's own activity: sign-ins plus uploads, matters, workflow runs and Assistant questions."""
from heapq import merge

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from custom_authentication.models import UserActivityLog

MAX_PAGE_SIZE = 100

BULK_EVENT = {
    "Completed": "WORKFLOW_COMPLETED",
    "Partially_Completed": "WORKFLOW_COMPLETED",
    "Failed": "WORKFLOW_FAILED",
    "Cancelled": "WORKFLOW_CANCELLED",
}


def _int(value, default, lo, hi):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _sources(user):
    """(queryset, timestamp field, row -> event dict) for every activity source."""
    from core.models import BulkJob, File, Matter
    from grid_documents_interrogation.models import Query

    return [
        (
            UserActivityLog.objects.filter(user=user),
            "timestamp",
            lambda r: {"event": r.event, "timestamp": r.timestamp, "metadata": r.metadata or {}},
        ),
        (
            File.objects.filter(user=user).exclude(document_type="deliverable"),
            "created_at",
            lambda f: {"event": "FILE_UPLOADED", "timestamp": f.created_at,
                       "metadata": {"filename": f.filename, "file_id": f.id, "matter": f.project_id}},
        ),
        (
            Matter.objects.filter(created_by=user),
            "created_at",
            lambda m: {"event": "PROJECT_CREATED", "timestamp": m.created_at,
                       "metadata": {"filename": m.name, "matter_id": str(m.id)}},
        ),
        (
            BulkJob.objects.filter(user=user),
            "created_at",
            lambda j: {"event": BULK_EVENT.get(j.status, "WORKFLOW_STARTED"), "timestamp": j.created_at,
                       "metadata": {"filename": j.get_job_type_display(), "job_id": str(j.id), "matter": j.project_id,
                                    "file_count": j.total_files, "status": j.status}},
        ),
        (
            Query.objects.filter(user=user),
            "created_at",
            lambda q: {"event": "CHAT_QUERY", "timestamp": q.created_at,
                       "metadata": {"query_preview": (q.query_text or "")[:120]}},
        ),
    ]


class MyActivityView(APIView):
    """GET /api/v1/auth/user/activity/?page=1&page_size=20 (newest first)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        page = _int(request.query_params.get("page"), 1, 1, 10_000)
        size = _int(request.query_params.get("page_size"), 20, 1, MAX_PAGE_SIZE)
        needed = page * size

        streams, total = [], 0
        for qs, field, to_event in _sources(request.user):
            total += qs.count()
            rows = qs.order_by(f"-{field}")[:needed]
            streams.append([to_event(r) for r in rows])

        merged = list(merge(*streams, key=lambda e: e["timestamp"], reverse=True))
        items = merged[(page - 1) * size: needed]
        total_pages = max(1, -(-total // size))
        return Response({"results": items, "total": total, "page": page, "page_size": size,
                         "total_pages": total_pages})
