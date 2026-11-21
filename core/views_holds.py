"""
Legal holds API (all scoped to the caller's organisation).

GET  /api/v1/core/legal-holds/?status=active|released|all      legal_hold.view
POST /api/v1/core/legal-holds/  {name, reason, matter_id?, file_ids?}   legal_hold.manage
POST /api/v1/core/legal-holds/<id>/release/  {reason}          legal_hold.manage
GET  /api/v1/core/legal-holds/check/?file_id=<id>              anyone who can view the document

Holds are never deleted, only released: they are the record of what was preserved and why.
"""
import logging

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import (get_file_for_user, is_platform_superuser, is_tenant_admin, tenant_id, user_has_perm)
from core.holds import holds_for
from core.models import File, LegalHold, Matter

logger = logging.getLogger(__name__)
MAX_FILES = 5000


def can_view(user):
    return is_platform_superuser(user) or is_tenant_admin(user) or user_has_perm(user, "legal_hold.view", "legal_hold.manage")


def can_manage(user):
    return is_platform_superuser(user) or is_tenant_admin(user) or user_has_perm(user, "legal_hold.manage")


def serialize(hold):
    return {"id": str(hold.id), "name": hold.name, "reason": hold.reason, "active": hold.active,
            "matter": {"id": str(hold.matter_id), "name": hold.matter.name} if hold.matter_id else None,
            "document_count": hold.files.count(),
            "created_by": getattr(hold.created_by, "email", None), "created_at": hold.created_at,
            "released_by": getattr(hold.released_by, "email", None), "released_at": hold.released_at,
            "release_reason": hold.release_reason}


class LegalHoldListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not can_view(request.user):
            return Response({"error": "You cannot view legal holds."}, status=status.HTTP_403_FORBIDDEN)
        tid = tenant_id(request.user)
        if tid is None:
            return Response({"error": "Your account is not part of an organisation."}, status=status.HTTP_400_BAD_REQUEST)
        holds = LegalHold.objects.filter(client_id=tid).select_related("matter", "created_by", "released_by")
        state = request.query_params.get("status", "active")
        if state == "active":
            holds = holds.filter(released_at__isnull=True)
        elif state == "released":
            holds = holds.filter(released_at__isnull=False)
        return Response({"results": [serialize(h) for h in holds[:500]], "can_manage": can_manage(request.user)})

    def post(self, request):
        user = request.user
        if not can_manage(user):
            return Response({"error": "Only administrators and compliance officers can place legal holds."},
                            status=status.HTTP_403_FORBIDDEN)
        tid = tenant_id(user)
        if tid is None:
            return Response({"error": "Your account is not part of an organisation."}, status=status.HTTP_400_BAD_REQUEST)
        name = str(request.data.get("name") or "").strip()[:255]
        if not name:
            return Response({"error": "Give the hold a name."}, status=status.HTTP_400_BAD_REQUEST)
        matter = None
        if request.data.get("matter_id"):
            try:
                matter = Matter.objects.get(pk=request.data["matter_id"], client_id=tid)
            except Exception:
                return Response({"error": "Matter not found."}, status=status.HTTP_404_NOT_FOUND)
        raw_ids = request.data.get("file_ids") or []
        if not isinstance(raw_ids, list) or len(raw_ids) > MAX_FILES:
            return Response({"error": f"file_ids must be a list of at most {MAX_FILES} ids."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            ids = list(dict.fromkeys(int(i) for i in raw_ids))
        except (TypeError, ValueError):
            return Response({"error": "file_ids must be document ids."}, status=status.HTTP_400_BAD_REQUEST)
        # Only this organisation's documents; one foreign or unknown id fails the request (never a partial hold).
        files = list(File.objects.filter(pk__in=ids, user__client_id=tid).values_list("pk", flat=True))
        if len(files) != len(ids):
            return Response({"error": "One or more documents were not found."}, status=status.HTTP_404_NOT_FOUND)
        if matter is None and not files:
            return Response({"error": "Choose a matter or at least one document to hold."}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            hold = LegalHold.objects.create(client_id=tid, name=name, reason=str(request.data.get("reason") or "")[:5000],
                                            matter=matter, created_by=user)
            if files:
                hold.files.set(files)
        logger.info("Legal hold placed: %s by user %s (matter=%s, documents=%d)", hold.id, user.pk, matter and matter.pk, len(files))
        from custom_authentication.views_admin import audit
        audit(request, "legal_hold.placed", "legal_hold", hold.id, hold.name, client_id=tid,
              matter=matter.name if matter else None, documents=len(files), reason=hold.reason)
        return Response(serialize(hold), status=status.HTTP_201_CREATED)


class LegalHoldReleaseView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not can_manage(request.user):
            return Response({"error": "Only administrators and compliance officers can release legal holds."},
                            status=status.HTTP_403_FORBIDDEN)
        hold = LegalHold.objects.filter(pk=pk, client_id=tenant_id(request.user)).first()
        if hold is None:
            return Response({"error": "Legal hold not found."}, status=status.HTTP_404_NOT_FOUND)
        if not hold.active:
            return Response({"error": "This legal hold is already released."}, status=status.HTTP_400_BAD_REQUEST)
        reason = str(request.data.get("reason") or "").strip()
        if not reason:
            return Response({"error": "Say why the hold is being released."}, status=status.HTTP_400_BAD_REQUEST)
        hold.released_at, hold.released_by, hold.release_reason = timezone.now(), request.user, reason[:5000]
        hold.save(update_fields=["released_at", "released_by", "release_reason"])
        logger.info("Legal hold released: %s by user %s", hold.id, request.user.pk)
        from custom_authentication.views_admin import audit
        audit(request, "legal_hold.released", "legal_hold", hold.id, hold.name, client_id=hold.client_id, reason=reason[:500])
        return Response(serialize(hold))


class LegalHoldCheckView(APIView):
    """Is this document under legal hold? For badges and disabled actions in the interface."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        document = get_file_for_user(request.user, request.query_params.get("file_id"))
        holds = list(holds_for(document).values_list("name", flat=True)) if can_view(request.user) else []
        on_hold = bool(holds) or holds_for(document).exists()
        return Response({"file_id": document.pk, "on_hold": on_hold, "holds": holds})
