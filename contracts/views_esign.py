"""
E-signature API (/api/v1/contracts/).

POST contracts/<id>/signature/ {provider: docusign|adobe_sign, signers: [{name, email, order}], message}
GET  contracts/<id>/signature/                      signature requests of the contract
POST signature/<id>/refresh/                         re-check the status now
POST signature/<id>/void/ {reason}
POST signature/webhook/docusign/                     DocuSign Connect (HMAC-verified)
POST signature/webhook/<docuseal|documenso|signudoc>/<token>/   organisation connections (unguessable address,
                                                     plus the provider's own signature or secret when configured)
GET|PUT signature/connections/                       the organisation's DocuSeal / Documenso / SignuDoc connections (admins)
POST signature/connections/<provider>/test/          check a connection
GET  signature/providers/                            the e-signature services this person can send with
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



# ── organisation connections: DocuSeal, Documenso, SignuDoc ─────────────────────────────────────────────────────
class OrgESignWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, provider, token):
        import hmac as _hmac
        from integrations.models import ESignConnection
        from integrations.providers.esign_open import ORG_PROVIDERS
        p = ORG_PROVIDERS.get(provider)
        if p is None or not token or len(token) > 64:
            return JsonResponse({"error": "unknown"}, status=404)
        connection = next((c for c in ESignConnection.objects.filter(provider=provider, webhook_token=token, enabled=True)
                           if _hmac.compare_digest(c.webhook_token, token)), None)
        if connection is None:
            return JsonResponse({"error": "unknown"}, status=404)
        if not p.webhook_ok(connection, request):
            return JsonResponse({"error": "invalid signature"}, status=401)
        try:
            envelope = p.webhook_envelope(json.loads(request.body or b"{}"))
        except (ValueError, AttributeError):
            envelope = None
        if envelope:
            ids = SignatureRequest.objects.filter(client_id=connection.client_id, provider=provider,
                                                  envelope_id=str(envelope)[:128], status__in=esign.OPEN).values_list("pk", flat=True)
            for pk in ids:
                esign.esign_refresh_task.delay(str(pk))
        return JsonResponse({"ok": True})


def _connection_out(c, provider):
    return {"provider": provider.key, "name": provider.name, "connected": c is not None and bool(c.api_token),
            "enabled": bool(c and c.enabled), "base_url": c.base_url if c else "",
            "token_configured": bool(c and c.api_token), "webhook_secret_configured": bool(c and c.webhook_secret),
            "webhook_url": esign.callback_url(provider.key, c) if c else "",
            "updated_at": c.updated_at if c else None, "updated_by": c.updated_by.email if c and c.updated_by else None}


def _admin_client(request):
    from core.access import is_tenant_admin, tenant_id
    if not is_tenant_admin(request.user):
        return None
    return tenant_id(request.user)


class ESignConnectionsView(APIView):
    """The organisation's connections to open-source e-signature services (administrators)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from integrations.models import ESignConnection
        from integrations.providers.esign_open import ORG_PROVIDERS
        client_id = _admin_client(request)
        if client_id is None:
            return _err("Only administrators can manage e-signature connections.", 403)
        rows = {c.provider: c for c in ESignConnection.objects.filter(client_id=client_id).select_related("updated_by")}
        return Response({"results": [_connection_out(rows.get(k), p) for k, p in ORG_PROVIDERS.items()],
                         "callbacks_configured": bool(esign.public_base())})

    def put(self, request):
        import secrets
        from integrations.models import ESignConnection
        from integrations.providers.esign_open import ORG_PROVIDERS, check_base_url
        from integrations.providers import ConnectorError
        client_id = _admin_client(request)
        if client_id is None:
            return _err("Only administrators can manage e-signature connections.", 403)
        p = ORG_PROVIDERS.get(str(request.data.get("provider") or ""))
        if p is None:
            return _err("Choose DocuSeal, Documenso or SignuDoc.")
        c = ESignConnection.objects.filter(client_id=client_id, provider=p.key).first()
        if request.data.get("remove"):
            if c is not None:
                c.delete()
            _audit(request, "esign.disconnected", p, client_id)
            return Response(_connection_out(None, p))
        try:
            base = check_base_url(str(request.data.get("base_url") or (c.base_url if c else "")))
        except ConnectorError as exc:
            return _err(str(exc))
        token = str(request.data.get("api_token") or "").strip()
        if c is None and not token:
            return _err(f"Paste the {p.name} API token.")
        if token and (len(token) > 1500 or any(ch.isspace() for ch in token)):
            return _err("That does not look like an API token.")
        if c is None:
            c = ESignConnection(client_id=client_id, provider=p.key, webhook_token=secrets.token_urlsafe(32))
        c.base_url = base
        if token:
            c.api_token = token
        if "webhook_secret" in request.data:
            c.webhook_secret = str(request.data.get("webhook_secret") or "").strip()[:500]
        if "enabled" in request.data:
            c.enabled = bool(request.data.get("enabled"))
        c.updated_by = request.user
        c.save()
        _audit(request, "esign.connected", p, client_id, base_url=base, enabled=c.enabled)
        return Response(_connection_out(c, p))


class ESignConnectionTestView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, provider):
        from integrations.models import ESignConnection
        from integrations.providers.esign_open import ORG_PROVIDERS, OrgApi
        client_id = _admin_client(request)
        if client_id is None:
            return _err("Only administrators can manage e-signature connections.", 403)
        p = ORG_PROVIDERS.get(provider)
        c = ESignConnection.objects.filter(client_id=client_id, provider=provider).first() if p else None
        if c is None:
            return _err("Save the connection first.")
        try:
            account = p.check(OrgApi(p, c))
        except Exception as exc:
            msg = str(exc) if exc.__class__.__name__ == "ConnectorError" else f"{p.name} could not be reached at {c.base_url}."
            return Response({"ok": False, "message": msg[:300]})
        return Response({"ok": True, "message": f"Connected to {account}."})


class ESignProvidersView(APIView):
    """The services this person can send with now: their own DocuSign/Adobe accounts and the organisation's
    connections. The panel offers only these, so people can switch when one service is unavailable."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from integrations.models import ConnectorAccount, ESignConnection
        from integrations.providers.esign_open import ORG_PROVIDERS
        own = set(ConnectorAccount.objects.filter(user=request.user, service__in=["docusign", "adobe_sign"])
                  .exclude(access_token="").values_list("service", flat=True))
        org = set(ESignConnection.objects.filter(client_id=getattr(request.user, "client_id", None), enabled=True)
                  .exclude(api_token="").values_list("provider", flat=True))
        catalogue = [("docusign", "DocuSign", "personal"), ("adobe_sign", "Adobe Acrobat Sign", "personal")] + \
                    [(k, p.name, "organisation") for k, p in ORG_PROVIDERS.items()]
        return Response({"results": [{"key": k, "name": n, "scope": scope, "available": (k in own) if scope == "personal" else (k in org)}
                                     for k, n, scope in catalogue]})


def _audit(request, action, provider, client_id, **changes):
    try:
        from custom_authentication.views_admin import audit
        audit(request, action, "esign_connection", provider.key, provider.name, client_id=client_id, changes=changes)
    except Exception:
        pass
