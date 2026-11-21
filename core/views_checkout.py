"""
Check-out and check-in.

POST /api/v1/core/files/<file_id>/checkout/  {note}   needs edit access; not while under legal hold
POST /api/v1/core/files/<file_id>/checkin/            the holder, or an administrator of the organisation
GET  /api/v1/core/files/<file_id>/checkout/           who has it checked out (anyone who can view it)
"""
import logging

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import can_access_file, get_file_for_user, is_platform_superuser, is_tenant_admin
from core.holds import assert_not_held
from core.models import DocumentCheckout

logger = logging.getLogger(__name__)


def _state(f):
    row = DocumentCheckout.objects.filter(file=f).select_related("user").first()
    return {"file_id": f.pk, "checked_out": bool(row), "by": row.user.email if row else None,
            "by_id": row.user_id if row else None, "since": row.checked_out_at if row else None,
            "note": row.note if row else ""}


class CheckoutView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, file_id):
        return Response(_state(get_file_for_user(request.user, file_id)))

    def post(self, request, file_id):
        f = get_file_for_user(request.user, file_id)
        if not can_access_file(request.user, f, "edit"):
            return Response({"error": "You can view but not edit this document."}, status=status.HTTP_403_FORBIDDEN)
        assert_not_held(f, "edit")
        try:
            with transaction.atomic():
                row, created = DocumentCheckout.objects.get_or_create(
                    file=f, defaults={"user": request.user, "note": str(request.data.get("note") or "")[:500]})
        except IntegrityError:
            row, created = DocumentCheckout.objects.get(file=f), False
        if not created and row.user_id != request.user.pk:
            return Response({"error": f"This document is already checked out by {row.user.email}."},
                            status=status.HTTP_409_CONFLICT)
        logger.info("File %s checked out by user %s", f.pk, request.user.pk)
        return Response(_state(f))


class CheckinView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, file_id):
        f = get_file_for_user(request.user, file_id)
        row = DocumentCheckout.objects.filter(file=f).first()
        if row is None:
            return Response(_state(f))
        forced = row.user_id != request.user.pk
        if forced and not (is_platform_superuser(request.user) or is_tenant_admin(request.user)):
            return Response({"error": "Only the person who checked it out or an administrator can check it in."},
                            status=status.HTTP_403_FORBIDDEN)
        row.delete()
        logger.info("File %s checked in by user %s%s", f.pk, request.user.pk, " (forced)" if forced else "")
        if forced:
            from custom_authentication.views_admin import audit
            audit(request, "document.forced_check_in", "document", f.pk, f.filename,
                  client_id=getattr(f.user, "client_id", None), holder_id=row.user_id)
        return Response(_state(f))
