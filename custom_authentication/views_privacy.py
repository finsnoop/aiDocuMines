"""Personal data export and erasure for platform users (custom_authentication/privacy.py)."""
import json

from django.http import HttpResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import CustomUser
from .views_admin import _forbidden, _is_admin, _managed, audit


def _download(data, user_id):
    body = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    response = HttpResponse(body, content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="personal-data-{user_id}.json"'
    response["Cache-Control"] = "private, no-store"
    return response


class MyPersonalDataView(APIView):
    """GET: everything the platform holds about the signed-in person (right of access and portability)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .privacy import personal_data
        audit(request, "privacy.export_own", "user", request.user.pk, client_id=request.user.client_id)
        return _download(personal_data(request.user), request.user.pk)


class PersonPersonalDataView(APIView):
    """GET: an administrator exports a person's data (to answer that person's request)."""
    permission_classes = [IsAuthenticated]

    def get(self, request, user_id):
        from .privacy import personal_data
        if not _is_admin(request.user):
            return _forbidden()
        target = _managed(request, user_id)
        audit(request, "privacy.export", "user", target.pk, client_id=target.client_id)
        return _download(personal_data(target), target.pk)


class ErasePersonView(APIView):
    """POST {confirm_email, transfer_to? (colleague's id or email)}: erase a person's personal data (privacy.erase)."""
    permission_classes = [IsAuthenticated]

    def post(self, request, user_id):
        from .privacy import ErasureRefused, erase
        if not _is_admin(request.user):
            return _forbidden()
        target = _managed(request, user_id)
        if str(request.data.get("confirm_email") or "").strip().lower() != target.email.lower():
            return Response({"error": "Type the person's email address exactly to confirm."}, status=400)
        transfer_to = None
        wanted = str(request.data.get("transfer_to") or "").strip()
        if wanted:
            transfer_to = (CustomUser.objects.filter(email__iexact=wanted).first() if "@" in wanted
                           else CustomUser.objects.filter(pk=int(wanted)).first() if wanted.isdigit() else None)
            if transfer_to is None:
                return Response({"error": "The colleague to receive the documents was not found."}, status=400)
        try:
            summary = erase(request.user, target, transfer_to=transfer_to)
        except ErasureRefused as exc:
            return Response({"error": str(exc)}, status=409)
        # Recorded without the email: the audit trail must not keep what was just erased.
        audit(request, "privacy.erase", "user", target.pk, label=f"former user #{target.pk}",
              client_id=target.client_id, **{k: v for k, v in summary.items() if isinstance(v, int)})
        return Response({"ok": True, "summary": summary})


class DataResidencyView(APIView):
    """GET: where this organisation's data is stored and which services process it (administrators;
    platform administrators with ?organisation=)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.access import is_platform_superuser, tenant_id
        from .models import Client
        from .privacy import residency_statement
        if not _is_admin(request.user):
            return _forbidden()
        org = request.query_params.get("organisation") if is_platform_superuser(request.user) else None
        client = Client.objects.filter(pk=int(org) if org and str(org).isdigit() else tenant_id(request.user)).first()
        if client is None:
            return Response({"error": "No organisation."}, status=404)
        return Response(residency_statement(client))
