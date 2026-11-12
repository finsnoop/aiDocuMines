"""
GET /api/v1/service-analytics/reports/?page=&page_size=&category=&search=

One feed of every piece of work run for the organisation (bulk workflows and
practice-area service runs), categorised for the Insights page.
"""
from django.db.models import Q
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import is_platform_superuser, is_tenant_admin, tenant_id

CATEGORIES = {
    "due_diligence": "Due diligence",
    "litigation": "Litigation and claims",
    "compliance": "Compliance and employment",
    "pii": "Privacy and redaction",
    "document_ops": "Document processing",
    "ai_analysis": "AI analysis",
    "financial": "Financial",
}
ICONS = {
    "pii": "fa-user-shield", "due_diligence": "fa-search-dollar", "litigation": "fa-gavel",
    "compliance": "fa-clipboard-check", "document_ops": "fa-file-alt", "ai_analysis": "fa-robot",
    "financial": "fa-chart-line",
}
PII_KEYS = ("pii", "redact", "anonym", "dsar")
DOC_OPS_KEYS = ("ocr", "translat", "structure", "redlin", "metadata", "version")


def categorise(kind: str) -> str:
    k = (kind or "").lower().replace("-", "_")
    if any(x in k for x in PII_KEYS):
        return "pii"
    if any(x in k for x in DOC_OPS_KEYS):
        return "document_ops"
    if k.startswith("pe_"):
        return "due_diligence"
    if k.startswith(("ca_", "ip_")):
        return "litigation"
    if k.startswith(("rc_", "le_")):
        return "compliance"
    if "wage" in k or "damages" in k or "financ" in k:
        return "financial"
    return "ai_analysis"


def _severity(status: str, failed: int = 0) -> str:
    s = (status or "").lower()
    if s in ("failed", "error"):
        return "Failed"
    if s == "partially_completed" or failed:
        return "Partial"
    if s in ("completed", "success"):
        return "Completed"
    if s in ("cancelled",):
        return "Cancelled"
    return "Running"


def _owner_filter(user, field_user="user", field_client="user__client_id"):
    if is_platform_superuser(user):
        return Q()
    tid = tenant_id(user)
    if is_tenant_admin(user) and tid is not None:
        return Q(**{field_client: tid})
    return Q(**{field_user: user})


def _bulk_reports(user, search):
    from core.models import BulkJob

    qs = BulkJob.objects.filter(_owner_filter(user)).order_by("-created_at")
    if search:
        qs = qs.filter(Q(project_id__icontains=search) | Q(job_type__icontains=search))
    for j in qs[:1000]:
        cat = categorise(j.job_type)
        name = j.get_job_type_display()
        yield {
            "report_id": f"bulk:{j.id}", "label": name, "category": cat, "category_label": CATEGORIES[cat],
            "icon": ICONS[cat], "title": f"{name} - {j.project_id}" if j.project_id else name,
            "summary": f"{j.processed_files} of {j.total_files} documents processed"
                       + (f", {j.failed_files} failed" if j.failed_files else ""),
            "status": j.status, "severity": _severity(j.status, j.failed_files),
            "timestamp": j.completed_at or j.created_at,
            "details": {"job_id": str(j.id), "matter": j.project_id, "total_files": j.total_files,
                        "processed_files": j.processed_files, "failed_files": j.failed_files,
                        "downloads": {"excel": f"/api/v1/core/bulk/{j.id}/download/?format=xlsx",
                                      "zip": f"/api/v1/core/bulk/{j.id}/download/?format=zip"}},
        }


def _vertical_reports(user, search):
    from class_actions.models import ServiceExecution as CA
    from ip_litigation.models import ServiceExecution as IP
    from labor_employment.models import ServiceExecution as LE
    from private_equity.models import ServiceExecution as PE
    from regulatory_compliance.models import ServiceExecution as RC

    tid = tenant_id(user)
    if is_platform_superuser(user):
        owner = Q()
    elif is_tenant_admin(user) and tid is not None:
        owner = Q(client_id=tid)
    else:
        owner = Q(user=user)
    for model in (PE, CA, LE, IP, RC):
        qs = model.objects.filter(owner).order_by("-started_at")
        if search:
            qs = qs.filter(Q(service_name__icontains=search) | Q(service_type__icontains=search))
        for e in qs[:500]:
            cat = categorise(e.service_type)
            yield {
                "report_id": f"{model._meta.app_label}:{e.id}", "label": e.service_name or e.service_type,
                "category": cat, "category_label": CATEGORIES[cat], "icon": ICONS[cat],
                "title": e.service_name or e.service_type,
                "summary": e.error_message or (f"{e.output_count} outputs ({e.output_type})" if e.output_count else ""),
                "status": e.status, "severity": _severity(e.status),
                "timestamp": e.completed_at or e.started_at,
                "details": {"service_type": e.service_type, "execution_time_seconds": e.execution_time_seconds,
                            "vertical": model._meta.app_label},
            }


class ServiceReportsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            page = max(1, int(request.query_params.get("page", 1)))
            size = max(1, min(100, int(request.query_params.get("page_size", 10))))
        except ValueError:
            page, size = 1, 10
        category = request.query_params.get("category") or "all"
        search = (request.query_params.get("search") or "").strip()[:200]

        reports = list(_bulk_reports(request.user, search)) + list(_vertical_reports(request.user, search))
        counts = {}
        for r in reports:
            counts[r["category"]] = counts.get(r["category"], 0) + 1
        if category != "all":
            reports = [r for r in reports if r["category"] == category]
        reports.sort(key=lambda r: r["timestamp"] or 0, reverse=True)

        total = len(reports)
        return Response({
            "results": reports[(page - 1) * size: page * size],
            "total": total, "page": page, "page_size": size, "total_pages": max(1, -(-total // size)),
            "categories": [{"id": k, "label": CATEGORIES[k], "count": counts[k]} for k in CATEGORIES if k in counts],
        })
