"""
GET /api/v1/documents/table/  one flat, sortable, filterable list of every document the user can open.

Query: page, page_size (max 100), search, project_id, service_id, extension,
       ordering (name|-name|size|-size|modified|-modified|matter|-matter), scope (all|mine|shared)

Built for very large libraries: rows come one page at a time from an indexed
ordering, and the total is counted exactly only up to 10,000 (then "10,000+"),
because exact counts over billions of rows are slow and nobody pages that far.
"""
from django.db.models import Q
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import accessible_files

COUNT_CAP = 10_000
MAX_PAGE_SIZE = 100
ORDERING = {
    "name": "filename", "-name": "-filename",
    "size": "file_size", "-size": "-file_size",
    "modified": "updated_at", "-modified": "-updated_at",
    "matter": "project_id", "-matter": "-project_id",
    "created": "created_at", "-created": "-created_at",
}


def _int(v, default, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


class DocumentTableView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        qp = request.query_params
        page = _int(qp.get("page"), 1, 1, 100_000)
        size = _int(qp.get("page_size"), 50, 1, MAX_PAGE_SIZE)

        qs = accessible_files(user).exclude(document_type="deliverable")
        scope = qp.get("scope", "all")
        if scope == "mine":
            qs = qs.filter(user=user)
        elif scope == "shared":
            qs = qs.exclude(user=user)
        search = (qp.get("search") or "").strip()[:200]
        if search:
            qs = qs.filter(Q(filename__icontains=search) | Q(project_id__icontains=search))
        for field in ("project_id", "service_id"):
            if qp.get(field):
                qs = qs.filter(**{field: qp[field][:255]})
        if qp.get("extension"):
            qs = qs.filter(extension__iexact=qp["extension"][:10])

        ordering = ORDERING.get(qp.get("ordering", ""), "-updated_at")
        start = (page - 1) * size
        rows = list(qs.select_related("user").order_by(ordering, "-id")[start: start + size + 1])
        has_next = len(rows) > size
        rows = rows[:size]

        capped = qs.order_by()[:COUNT_CAP + 1].count() if page == 1 or qp.get("with_count") else None
        total = None if capped is None else min(capped, COUNT_CAP)

        return Response({
            "results": [{
                "id": f.id,
                "filename": f.filename,
                "extension": (f.extension or "").lower(),
                "file_type": f.file_type,
                "file_size": f.file_size,
                "matter": f.project_id,
                "service": f.service_id,
                "document_type": f.document_type,
                "status": f.status,
                "owner": {"id": f.user_id, "name": (f.user.username or f.user.email) if f.user else ""},
                "is_mine": f.user_id == user.id,
                "created_at": f.created_at,
                "updated_at": f.updated_at,
            } for f in rows],
            "page": page,
            "page_size": size,
            "has_next": has_next,
            "count": total,
            "count_is_capped": capped is not None and capped > COUNT_CAP,
        })


class DocumentFacetsView(APIView):
    """
    GET /api/v1/documents/table/facets/  matters, services and file types for the table's filters (bounded).
    ?project_id=X narrows services and file types to that project (used by the Assistant's topic picker).
    """
    permission_classes = [IsAuthenticated]
    LIMIT = 200

    def get(self, request):
        qs = accessible_files(request.user).exclude(document_type="deliverable").order_by()
        project = (request.query_params.get("project_id") or "").strip()[:255]
        if project:
            qs = qs.filter(project_id=project)

        def distinct(field):
            return sorted({v for v in qs.values_list(field, flat=True).distinct()[: self.LIMIT] if v})

        return Response({
            "matters": distinct("project_id"),
            "services": distinct("service_id"),
            "extensions": [e.lower() for e in distinct("extension")],
        })
