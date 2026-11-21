"""
Retention schedules and disposition approvals (all scoped to the caller's organisation).

GET  /api/v1/core/retention/schedules/                      retention.view
POST /api/v1/core/retention/schedules/  {name, retain_days, matter_id?, document_type?, service_id?}   retention.manage
POST /api/v1/core/retention/schedules/<id>/deactivate/      retention.manage
POST /api/v1/core/retention/schedules/<id>/review/          retention.manage   propose a batch now
GET  /api/v1/core/retention/batches/?status=pending          retention.view
GET  /api/v1/core/retention/batches/<id>/                   retention.view     with its documents
POST /api/v1/core/retention/batches/<id>/approve/           retention.manage   destroys (holds and check-outs kept)
POST /api/v1/core/retention/batches/<id>/reject/  {note}    retention.manage
"""
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core import retention
from core.access import is_platform_superuser, is_tenant_admin, tenant_id, user_has_perm
from core.models import DispositionBatch, Matter, RetentionSchedule

MAX_DAYS = 365 * 100


def can_view(user):
    return is_platform_superuser(user) or is_tenant_admin(user) or user_has_perm(user, "retention.view", "retention.manage")


def can_manage(user):
    return is_platform_superuser(user) or is_tenant_admin(user) or user_has_perm(user, "retention.manage")


def _err(message, code):
    return Response({"error": message}, status=code)


def _guard(request, manage=False):
    if not (can_manage(request.user) if manage else can_view(request.user)):
        return _err("Only administrators and compliance officers can " + ("change" if manage else "view") + " retention.",
                    status.HTTP_403_FORBIDDEN)
    if tenant_id(request.user) is None:
        return _err("Your account is not part of an organisation.", status.HTTP_400_BAD_REQUEST)
    return None


def schedule_out(s):
    return {"id": str(s.id), "name": s.name, "retain_days": s.retain_days, "active": s.active,
            "matter": {"id": str(s.matter_id), "name": s.matter.name} if s.matter_id else None,
            "document_type": s.document_type, "service_id": s.service_id,
            "created_by": getattr(s.created_by, "email", None), "created_at": s.created_at}


def batch_out(b, items=False):
    data = {"id": str(b.id), "schedule": {"id": str(b.schedule_id), "name": b.schedule.name}, "status": b.status,
            "created_at": b.created_at, "decided_by": getattr(b.decided_by, "email", None), "decided_at": b.decided_at,
            "note": b.note, "document_count": b.items.count()}
    if items:
        data["items"] = list(b.items.values("file_ref", "filename", "project_id", "created", "outcome")[:2000])
    return data


class ScheduleListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if (denied := _guard(request)):
            return denied
        rows = RetentionSchedule.objects.filter(client_id=tenant_id(request.user)).select_related("matter", "created_by")
        return Response({"results": [schedule_out(s) for s in rows], "can_manage": can_manage(request.user)})

    def post(self, request):
        if (denied := _guard(request, manage=True)):
            return denied
        tid = tenant_id(request.user)
        name = str(request.data.get("name") or "").strip()[:255]
        try:
            days = int(request.data.get("retain_days"))
        except (TypeError, ValueError):
            days = 0
        if not name or not 1 <= days <= MAX_DAYS:
            return _err("Give the schedule a name and a retention period of at least one day.", status.HTTP_400_BAD_REQUEST)
        matter = None
        if request.data.get("matter_id"):
            matter = Matter.objects.filter(pk=request.data["matter_id"], client_id=tid).first()
            if matter is None:
                return _err("Matter not found.", status.HTTP_404_NOT_FOUND)
        s = RetentionSchedule.objects.create(client_id=tid, name=name, retain_days=days, matter=matter,
                                             document_type=str(request.data.get("document_type") or "")[:100],
                                             service_id=str(request.data.get("service_id") or "")[:255],
                                             created_by=request.user)
        from custom_authentication.views_admin import audit
        audit(request, "retention.schedule_created", "retention_schedule", s.id, s.name, client_id=tid, retain_days=days,
              matter=matter.name if matter else None)
        return Response(schedule_out(s), status=status.HTTP_201_CREATED)


class ScheduleActionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk, action):
        if (denied := _guard(request, manage=True)):
            return denied
        s = RetentionSchedule.objects.filter(pk=pk, client_id=tenant_id(request.user)).select_related("matter").first()
        if s is None:
            return _err("Schedule not found.", status.HTTP_404_NOT_FOUND)
        if action == "deactivate":
            s.active = False
            s.save(update_fields=["active"])
            return Response(schedule_out(s))
        if action == "review":
            batch = retention.propose(s)
            return Response({"batch": batch_out(batch) if batch else None,
                             "message": None if batch else "Nothing is due, or a batch is already awaiting a decision."})
        return _err("Unknown action.", status.HTTP_404_NOT_FOUND)


class BatchListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if (denied := _guard(request)):
            return denied
        rows = DispositionBatch.objects.filter(client_id=tenant_id(request.user)).select_related("schedule", "decided_by")
        if request.query_params.get("status") in ("pending", "approved", "rejected"):
            rows = rows.filter(status=request.query_params["status"])
        return Response({"results": [batch_out(b) for b in rows[:200]], "can_manage": can_manage(request.user)})


class BatchDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _batch(self, request):
        return DispositionBatch.objects.filter(pk=self.kwargs["pk"], client_id=tenant_id(request.user)) \
            .select_related("schedule", "decided_by").first()

    def get(self, request, pk):
        if (denied := _guard(request)):
            return denied
        b = self._batch(request)
        return Response(batch_out(b, items=True)) if b else _err("Batch not found.", status.HTTP_404_NOT_FOUND)

    def post(self, request, pk, action):
        if (denied := _guard(request, manage=True)):
            return denied
        b = self._batch(request)
        if b is None:
            return _err("Batch not found.", status.HTTP_404_NOT_FOUND)
        if b.status != "pending":
            return _err("This batch has already been decided.", status.HTTP_400_BAD_REQUEST)
        from custom_authentication.views_admin import audit
        if action == "approve":
            counts = retention.carry_out(b, request.user)
            audit(request, "retention.disposition_approved", "disposition_batch", b.id, b.schedule.name,
                  client_id=b.client_id, outcomes=counts)
            return Response({**batch_out(b, items=True), "outcomes": counts})
        if action == "reject":
            retention.reject(b, request.user, str(request.data.get("note") or ""))
            audit(request, "retention.disposition_rejected", "disposition_batch", b.id, b.schedule.name, client_id=b.client_id)
            return Response(batch_out(b, items=True))
        return _err("Unknown action.", status.HTTP_404_NOT_FOUND)
