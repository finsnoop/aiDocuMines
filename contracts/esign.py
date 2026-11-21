"""
E-signature for contracts: send, follow, complete, void.

* Sending needs: the contract in the signature stage, its document readable by the sender, and the sender's own
  connected DocuSign or Adobe Sign account (Integrations).
* Status is only ever taken from the provider's API with the sender's credentials. Webhooks (DocuSign Connect
  with HMAC, Adobe Sign with client-id echo) and a periodic check just trigger that re-read.
* On completion the signed PDF and the certificate are stored next to the contract document (same matter and
  owner, folder tree, access re-checked at saving), the signed copy becomes the contract's document and the
  contract becomes active when its start date is set.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)
MAX_SIGNERS = 20
MAX_PDF_BYTES = 25 * 1024 * 1024
OPEN = ("sending", "sent", "delivered")


class ESignError(ValueError):
    """A refusal or failure that is safe to show."""


def provider_for(key):
    from integrations.providers import get_provider
    from integrations.providers.esign import ESignProvider
    p = get_provider(key)
    if not isinstance(p, ESignProvider):
        raise ESignError("Choose DocuSign or Adobe Acrobat Sign.")
    return p


def api_for(user, provider):
    from integrations.models import ConnectorAccount
    from integrations.providers import Api
    account = ConnectorAccount.objects.filter(user=user, service=provider.key).first()
    if account is None or not account.access_token:
        raise ESignError(f"Connect your {provider.name} account under Integrations first.")
    return Api(provider, account)


def clean_signers(raw):
    if not isinstance(raw, list) or not raw or len(raw) > MAX_SIGNERS:
        raise ESignError(f"Add between 1 and {MAX_SIGNERS} signers.")
    out, seen = [], set()
    for i, s in enumerate(raw):
        email = str((s or {}).get("email") or "").strip().lower()
        name = str((s or {}).get("name") or "").strip()[:100]
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or not name:
            raise ESignError("Each signer needs a name and a valid email address.")
        if email in seen:
            raise ESignError(f"{email} is listed twice.")
        seen.add(email)
        try:
            order = max(int((s or {}).get("order") or i + 1), 1)
        except (TypeError, ValueError):
            order = i + 1
        out.append({"name": name, "email": email, "order": order, "status": "", "signed_at": None})
    return out


def callback_url(provider_key):
    base = (os.getenv("ESIGN_PUBLIC_API_URL") or os.getenv("INTEGRATIONS_PUBLIC_API_URL") or "").strip().rstrip("/")
    return f"{base}/api/v1/contracts/signature/webhook/{provider_key}/" if base else ""


def send(contract, user, provider_key, signers, message=""):
    from core.access import can_access_file
    from .lifecycle import record
    from .models import SignatureRequest
    if contract.stage != "signature":
        raise ESignError("Move the contract to 'Out for signature' first.")
    doc = contract.document
    if doc is None or not can_access_file(user, doc, "process"):
        raise ESignError("Attach the contract document first.")
    if contract.signature_requests.filter(status__in=OPEN).exists():
        raise ESignError("This contract is already out for signature. Void that request first.")
    provider = provider_for(provider_key)
    api = api_for(user, provider)
    signers = clean_signers(signers)
    try:
        size = os.path.getsize(doc.filepath)
        if size > MAX_PDF_BYTES:
            raise ESignError("The document is too large to send for signature (25 MB at most).")
        with open(doc.filepath, "rb") as fh:
            data = fh.read()
    except OSError:
        raise ESignError("The contract document could not be read.")
    request = SignatureRequest.objects.create(client_id=contract.client_id, contract=contract, provider=provider.key,
                                              signers=signers, source_file=doc, sent_by=user)
    try:
        request.envelope_id = provider.send(api, name=contract.title, filename=doc.filename, pdf_bytes=data,
                                            signers=signers, message=message, callback_url=callback_url(provider.key))
        request.status = "sent"
    except Exception as exc:
        request.status, request.error = "error", str(exc)[:500] if exc.__class__.__name__ == "ConnectorError" else "Sending failed."
        request.save(update_fields=["status", "error"])
        raise ESignError(request.error)
    request.save(update_fields=["envelope_id", "status"])
    record(contract, "signature", user, note=f"Sent for signature with {provider.name}",
           signers=[s["email"] for s in signers], envelope=request.envelope_id)
    return request


def _store(request, content, suffix):
    """Write provider bytes next to the contract document and register them (access re-checked)."""
    from core.utils import register_generated_file
    doc = request.source_file
    stem = os.path.splitext(doc.filename)[0][:120]
    directory = os.path.join(os.path.dirname(doc.filepath) or settings.MEDIA_ROOT, "signed")
    media = os.path.realpath(settings.MEDIA_ROOT)
    if not os.path.realpath(directory).startswith(media + os.sep):
        directory = os.path.join(settings.MEDIA_ROOT, "signed", str(request.client_id))
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{stem}-{suffix}-{str(request.pk)[:8]}.pdf")
    with open(path, "wb") as fh:
        fh.write(content)
    return register_generated_file(path, doc.user, None, doc.project_id, doc.service_id, folder_name="signed",
                                   source_file=doc, actor=request.sent_by, permission="document.view")


def refresh(request):
    """Re-read the provider's status; finish the contract when signed. Returns the new status."""
    from .lifecycle import _move, record
    from .models import SignatureRequest
    if request.status not in OPEN or not request.envelope_id or request.sent_by is None:
        return request.status
    provider = provider_for(request.provider)
    api = api_for(request.sent_by, provider)
    state = provider.status(api, request.envelope_id)
    request.checked_at = timezone.now()
    by_email = {s.get("email", "").lower(): s for s in state.get("signers", [])}
    request.signers = [{**s, "status": by_email.get(s["email"], {}).get("status", s.get("status", "")),
                        "signed_at": by_email.get(s["email"], {}).get("signed_at", s.get("signed_at"))} for s in request.signers]
    new = state.get("status", "sent")
    if new == "completed":
        signed = _store(request, provider.signed_pdf(api, request.envelope_id), "signed")
        cert_bytes = provider.certificate(api, request.envelope_id)
        cert = _store(request, cert_bytes, "certificate") if cert_bytes else None
        signers, checked = request.signers, request.checked_at
        with transaction.atomic():
            request = SignatureRequest.objects.select_for_update().get(pk=request.pk)
            if request.status == "completed":
                return request.status
            request.signers, request.checked_at = signers, checked   # keep who signed and when
            request.status, request.completed_at = "completed", timezone.now()
            request.signed_file, request.certificate_file = signed, cert
            request.save()
            contract = request.contract
            contract.document = signed
            contract.save(update_fields=["document", "updated_at"])
            record(contract, "signature", None, note=f"Signed by all parties via {provider.name}", envelope=request.envelope_id)
            if contract.stage == "signature" and contract.start_date:
                _move(contract, "active", None, note="Signed by all parties.")
        return "completed"
    if new in ("declined", "voided") and request.status != new:
        request.status = new
        request.save(update_fields=["status", "signers", "checked_at"])
        record(request.contract, "signature", None, note=f"Signature request {new} at {provider.name}", envelope=request.envelope_id)
        return new
    request.status = new if new in OPEN else request.status
    request.save(update_fields=["status", "signers", "checked_at"])
    return request.status


def void(request, user, reason):
    from .lifecycle import record
    if request.status not in OPEN:
        raise ESignError("Only an open signature request can be voided.")
    provider = provider_for(request.provider)
    try:
        provider.void(api_for(user if user.pk == request.sent_by_id else request.sent_by, provider), request.envelope_id, reason)
    except ESignError:
        raise
    except Exception as exc:
        raise ESignError(str(exc)[:300] if exc.__class__.__name__ == "ConnectorError" else "Voiding failed.")
    request.status = "voided"
    request.save(update_fields=["status"])
    record(request.contract, "signature", user, note=f"Signature request voided: {reason}"[:500], envelope=request.envelope_id)


def docusign_signature_ok(body: bytes, header_value: str) -> bool:
    secret = os.getenv("DOCUSIGN_CONNECT_HMAC_KEY", "")
    if not secret or not header_value:
        return False
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, header_value.strip())


@shared_task(name="contracts.esign_refresh", soft_time_limit=300, time_limit=360)
def esign_refresh_task(request_id, user_id=None):
    from .models import SignatureRequest
    r = SignatureRequest.objects.select_related("contract", "source_file", "source_file__user", "sent_by").filter(pk=request_id).first()
    if r is None:
        return None
    try:
        return refresh(r)
    except Exception:
        logger.warning("E-signature status check failed for %s", request_id, exc_info=True)
        return "error"


@shared_task(name="contracts.esign_sweep", ignore_result=True, soft_time_limit=600, time_limit=660)
def esign_sweep():
    """Safety net for missed webhooks: re-check open requests not checked in the last 30 minutes."""
    from django.db.models import Q
    from .models import SignatureRequest
    stale = timezone.now() - timedelta(minutes=30)
    for pk in SignatureRequest.objects.filter(status__in=["sent", "delivered"]).filter(
            Q(checked_at__isnull=True) | Q(checked_at__lt=stale)).values_list("pk", flat=True)[:200]:
        esign_refresh_task.delay(str(pk))
