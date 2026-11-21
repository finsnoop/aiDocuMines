"""
Audit trail export and verification (organisation administrators; platform superusers with ?organisation=).

GET /api/v1/auth/admin/audit/export/?start=YYYY-MM-DD&end=YYYY-MM-DD&as=csv|jsonl&types=admin,sign_in,assistant,document,ai
    One file with every recorded action in the period: administrative and governance changes, sign-ins,
    assistant questions, document actions and every AI call (model, tokens, key; never content).
    The response carries X-Content-SHA256 so the copy can be checked later; the export itself is audited.
GET /api/v1/auth/admin/audit/verify/
    Checks the tamper-evident hash chain of the organisation's administrative audit events.
"""
import csv
import datetime as dt
import hashlib
import io
import json

from django.http import Http404, HttpResponse
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import is_platform_superuser, is_tenant_admin, tenant_id
from .audit_chain import verify
from .models import AdminAuditEvent, UserActivityLog
from .views_admin import audit

TYPES = ("admin", "sign_in", "assistant", "document", "ai")
MAX_DAYS = 400
MAX_ROWS = 100_000
COLUMNS = ["at", "type", "action", "actor", "target", "details", "ip", "entry_hash"]


def _org(request):
    if not (is_platform_superuser(request.user) or is_tenant_admin(request.user)):
        return None
    org = request.query_params.get("organisation") if is_platform_superuser(request.user) else None
    tid = int(org) if org and str(org).isdigit() else tenant_id(request.user)
    if not tid:
        raise Http404("No organisation")
    return tid


def rows(tid, since, until, types):
    """Normalised audit rows for one organisation in [since, until), oldest first."""
    out = []
    if "admin" in types:
        for e in AdminAuditEvent.objects.filter(client_id=tid, created_at__gte=since, created_at__lt=until) \
                .select_related("actor").order_by("id")[:MAX_ROWS]:
            out.append({"at": e.created_at, "type": "admin", "action": e.action,
                        "actor": e.actor.email if e.actor else "system", "target": e.target_label or e.target_id,
                        "details": e.details, "ip": e.ip_address, "entry_hash": e.entry_hash})
    events = []
    if "sign_in" in types:
        events += ["LOGIN", "LOGOUT", "PASSWORD_RESET"]
    if "assistant" in types:
        events += ["CHAT_QUERY"]
    if events:
        for a in UserActivityLog.objects.filter(user__client_id=tid, event__in=events, timestamp__gte=since,
                                                timestamp__lt=until).select_related("user").order_by("timestamp")[:MAX_ROWS]:
            meta = {k: v for k, v in (a.metadata or {}).items() if k != "query_preview"}
            out.append({"at": a.timestamp, "type": "assistant" if a.event == "CHAT_QUERY" else "sign_in",
                        "action": a.event.lower(), "actor": a.user.email, "target": meta.get("context_label", ""),
                        "details": meta, "ip": meta.get("ip"), "entry_hash": ""})
    if "document" in types:
        from document_operations.models import FileAuditLog
        for f in FileAuditLog.objects.filter(file__user__client_id=tid, timestamp__gte=since, timestamp__lt=until) \
                .select_related("user", "file").order_by("timestamp")[:MAX_ROWS]:
            out.append({"at": f.timestamp, "type": "document", "action": f.action,
                        "actor": f.user.email if f.user else "system", "target": f"{f.file.filename} (#{f.file_id})",
                        "details": f.extra or {}, "ip": None, "entry_hash": ""})
    if "ai" in types:
        from cost_centre.models import EventLog
        for e in EventLog.objects.filter(tenant_id=tid, created_at__gte=since, created_at__lt=until,
                                         service_type="payable").select_related("user").order_by("created_at")[:MAX_ROWS]:
            m = e.metadata or {}
            out.append({"at": e.created_at, "type": "ai", "action": "ai.call", "actor": e.user.email if e.user else "system",
                        "target": m.get("model", ""), "ip": None, "entry_hash": "",
                        "details": {k: m.get(k) for k in ("provider", "model", "task", "key", "prompt_tokens",
                                                          "completion_tokens", "duration_ms") if k in m} | {"tokens": e.tokens_used}})
    out.sort(key=lambda r: r["at"])
    return out[:MAX_ROWS]


def render(data, fmt):
    if fmt == "jsonl":
        return "".join(json.dumps({**r, "at": r["at"].isoformat()}, default=str, sort_keys=True) + "\n" for r in data).encode()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    for r in data:
        w.writerow([r["at"].isoformat(), r["type"], r["action"], r["actor"], r["target"],
                    json.dumps(r["details"], default=str, sort_keys=True), r["ip"] or "", r["entry_hash"]])
    return buf.getvalue().encode()


class AuditExportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tid = _org(request)
        if tid is None:
            return Response({"error": "Only administrators can export the audit trail."}, status=403)
        qp = request.query_params
        today = timezone.localdate()
        try:
            first = dt.date.fromisoformat(qp.get("start")) if qp.get("start") else today - dt.timedelta(days=30)
            last = dt.date.fromisoformat(qp.get("end")) if qp.get("end") else today
        except ValueError:
            return Response({"error": "Dates must be YYYY-MM-DD."}, status=400)
        if last < first or (last - first).days > MAX_DAYS:
            return Response({"error": f"Choose a period of at most {MAX_DAYS} days."}, status=400)
        fmt = qp.get("as", "csv")   # "format" is reserved by the API framework for its own renderers
        if fmt not in ("csv", "jsonl"):
            return Response({"error": "as must be csv or jsonl."}, status=400)
        types = [t for t in str(qp.get("types") or ",".join(TYPES)).split(",") if t in TYPES] or list(TYPES)
        tz = timezone.get_current_timezone()
        since = timezone.make_aware(dt.datetime.combine(first, dt.time.min), tz)
        until = timezone.make_aware(dt.datetime.combine(last + dt.timedelta(days=1), dt.time.min), tz)
        data = rows(tid, since, until, set(types))
        body = render(data, fmt)
        digest = hashlib.sha256(body).hexdigest()
        audit(request, "audit.exported", "organisation", tid, client_id=tid, start=first.isoformat(), end=last.isoformat(),
              types=types, rows=len(data), sha256=digest, format=fmt)
        name = f"audit-{first.isoformat()}-to-{last.isoformat()}.{fmt}"
        response = HttpResponse(body, content_type="text/csv; charset=utf-8" if fmt == "csv" else "application/x-ndjson")
        response["Content-Disposition"] = f'attachment; filename="{name}"'
        response["X-Content-SHA256"] = digest
        response["X-Audit-Rows"] = str(len(data))
        response["Access-Control-Expose-Headers"] = "Content-Disposition, X-Content-SHA256, X-Audit-Rows"
        response["Cache-Control"] = "private, no-store"
        return response


class AuditVerifyView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tid = _org(request)
        if tid is None:
            return Response({"error": "Only administrators can verify the audit trail."}, status=403)
        return Response(verify(tid))
