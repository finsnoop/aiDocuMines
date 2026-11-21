"""
E-signature API (/api/v1/contracts/).

POST contracts/<id>/signature/ {provider: docusign|adobe_sign, signers: [{name, email, order}], message}
GET  contracts/<id>/signature/                      signature requests of the contract
POST signature/<id>/refresh/                         re-check the status now
POST signature/<id>/void/ {reason}
POST signature/webhook/docusign/                     DocuSign Connect (HMAC-verified)
GET|POST signature/webhook/adobe_sign/               Adobe Acrobat Sign (client-id verified and echoed)
Webhooks never change anything themselves: they queue a re-read of the status from the provider's API.
"""
import json
import os

from django.http import JsonResponse
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import access, esign
from .models import SignatureRequest
from .views import _contract, _err


def request_out(r):
    return {"id": str(r.id), "provider": r.provider, "status": r.status, "status_label": r.get_status_display(),
            "signers": r.signers, "error": r.error, "created_at": r.created_at, "completed_at": r.completed_at,
            "sent_by": r.sent_by.email if r.sent_by else None,
            "signed_file": {"id": r.signed_file_id, "filename": r.signed_file.filename} if r.signed_file_id else None,
            "certificate_file": {"id": r.certificate_file_id, "filename": r.certificate_file.filename} if r.certificate_file_id else None}


class ContractSignatureView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        c = _contract(request.user, pk)
        if c is None:
            return _err("Contract not found.", 404)
        rows = c.signature_requests.select_related("sent_by", "signed_file", "certificate_file")[:20]
        return Response({"results": [request_out(r) for r in rows]})

    def post(self, request, pk):
        c = _contract(request.user, pk)
        if c is None:
            return _err("Contract not found.", 404)
        if not access.can_edit(request.user, c):
            return _err("Only the owner or a contract manager can send this contract for signature.", 403)
        try:
            r = esign.send(c, request.user, str(request.data.get("provider") or ""), request.data.get("signers"),
                           str(request.data.get("message") or "")[:2000])
        except esign.ESignError as exc:
            return _err(str(exc))
        return Response(request_out(r), status=201)


def _request(user, pk):
    r = SignatureRequest.objects.select_related("contract", "sent_by", "signed_file", "certificate_file",
                                                "source_file", "source_file__user").filter(pk=pk).first()
    if r is None or not access.can_view(user, r.contract):
        return None
    return r


class SignatureRefreshView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        r = _request(request.user, pk)
        if r is None:
            return _err("Signature request not found.", 404)
        try:
            esign.refresh(r)
        except esign.ESignError as exc:
            return _err(str(exc))
        except Exception:
            return _err("The e-signature service could not be reached. Try again shortly.", 502)
        return Response(request_out(_request(request.user, pk)))


class SignatureVoidView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        r = _request(request.user, pk)
        if r is None:
            return _err("Signature request not found.", 404)
        if not access.can_edit(request.user, r.contract):
            return _err("Only the owner or a contract manager can void this request.", 403)
        reason = str(request.data.get("reason") or "").strip()
        if not reason:
            return _err("Give a reason for voiding.")
        try:
            esign.void(r, request.user, reason)
        except esign.ESignError as exc:
            return _err(str(exc))
        return Response(request_out(r))


def _queue_for(provider, envelope_id):
    ids = SignatureRequest.objects.filter(provider=provider, envelope_id=str(envelope_id)[:128], status__in=esign.OPEN) \
        .values_list("pk", flat=True)
    for pk in ids:
        esign.esign_refresh_task.delay(str(pk))


class DocuSignWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        body = request.body
        if not esign.docusign_signature_ok(body, request.META.get("HTTP_X_DOCUSIGN_SIGNATURE_1", "")):
            return JsonResponse({"error": "invalid signature"}, status=401)
        try:
            payload = json.loads(body or b"{}")
            envelope = (payload.get("data") or {}).get("envelopeId") or payload.get("envelopeId")
        except (ValueError, AttributeError):
            envelope = None
        if envelope:
            _queue_for("docusign", envelope)
        return JsonResponse({"ok": True})


class AdobeSignWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def _verified(self, request):
        expected = os.getenv("ADOBE_SIGN_CLIENT_ID", "")
        got = request.META.get("HTTP_X_ADOBESIGN_CLIENTID", "")
        return bool(expected) and got == expected

    def _echo(self, request, body):
        response = JsonResponse(body)
        response["X-AdobeSign-ClientId"] = os.getenv("ADOBE_SIGN_CLIENT_ID", "")
        return response

    def get(self, request):       # webhook registration check
        if not self._verified(request):
            return JsonResponse({"error": "unknown client"}, status=401)
        return self._echo(request, {"xAdobeSignClientId": os.getenv("ADOBE_SIGN_CLIENT_ID", "")})

    def post(self, request):
        if not self._verified(request):
            return JsonResponse({"error": "unknown client"}, status=401)
        try:
            payload = json.loads(request.body or b"{}")
            agreement = (payload.get("agreement") or {}).get("id")
        except (ValueError, AttributeError):
            agreement = None
        if agreement:
            _queue_for("adobe_sign", agreement)
        return self._echo(request, {"xAdobeSignClientId": os.getenv("ADOBE_SIGN_CLIENT_ID", "")})
