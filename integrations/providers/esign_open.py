"""
Open-source e-signature services: DocuSeal, Documenso and SignuDoc.

Unlike DocuSign and Adobe Sign (each sender's own OAuth account), these are connected once per organisation by its
administrator (integrations.models.ESignConnection): the service's API address, an API token, and optionally the
webhook secret the service gives you. They usually run on the organisation's or our own servers.

Every provider here implements the same operations as integrations/providers/esign.py (send, status, signed_pdf,
certificate, void), so contracts/esign.py treats all five providers alike. Status is always re-read from the
provider's API; webhooks only trigger that re-read (and are verified where the provider signs them).

Signature placement: a "Signatures" page is appended to the contract with one labelled box per signer, and each
signer's signature field is placed in their box. The contract's own pages are never altered.

API references (checked November 2026):
  DocuSeal   X-Auth-Token header; POST /submissions/pdf (DocuSeal Pro or cloud: the free self-hosted edition's API
             only sends from templates), GET /submissions/{id}, DELETE /submissions/{id} (archive).
  Documenso  Authorization: <api token>; v2: POST /envelope/create (multipart payload + files), POST /envelope/distribute,
             GET /envelope/{id}, GET /envelope/item/{itemId}/download?version=signed, POST /envelope/cancel;
             webhooks carry the secret in X-Documenso-Secret.
  SignuDoc   Authorization: Bearer sdk_...; POST /envelopes (multipart file + data), GET /envelopes/{id},
             GET /envelopes/{id}/files/signed|certificate, POST /envelopes/{id}/void; Standard Webhooks signatures.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import time
from urllib.parse import urlparse

from .base import ConnectorError, http_request
from .esign import ESignProvider

SIG_PAGE_TITLE = "Signatures"
BOXES_PER_PAGE = 8
# Hosts reachable over plain http: services on our own private network (docker service names), never the internet.
INTERNAL_HOSTS = {h.strip() for h in os.getenv("ESIGN_INTERNAL_HOSTS", "docuseal,documenso,signudoc").split(",") if h.strip()}


def check_base_url(url: str) -> str:
    """The service address, cleaned; https unless it is one of our own internal service names."""
    url = (url or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ConnectorError("Enter the service's API address, for example https://sign.example.com/api.")
    if parsed.scheme == "http" and parsed.hostname not in INTERNAL_HOSTS:
        raise ConnectorError("The address must use https (plain http is only allowed for services on our own network).")
    if parsed.username or parsed.password:
        raise ConnectorError("Do not put credentials in the address; use the API token field.")
    return url


class OrgApi:
    """Authenticated calls to one organisation's connection (base address + token)."""

    def __init__(self, provider: "OpenESignProvider", connection):
        self.provider, self.connection = provider, connection
        self.base = check_base_url(connection.base_url)

    def url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def request(self, method: str, path: str, *, headers: dict | None = None, **kw):
        h = {**self.provider.auth_headers(self.connection.api_token or ""), **(headers or {})}
        return http_request(method, self.url(path), headers=h, allow_redirects=False, **kw)

    def fetch_file(self, url: str) -> bytes:
        """Download a file link the provider returned, only from the provider's own host."""
        target, own = urlparse(url), urlparse(self.base)
        allowed = {own.hostname} | set(self.provider.file_hosts)
        if target.scheme not in ("https", "http") or target.hostname not in allowed:
            raise ConnectorError("The e-signature service returned a file link on another host; it was not downloaded.")
        if target.scheme == "http" and target.hostname not in INTERNAL_HOSTS:
            raise ConnectorError("The e-signature service returned an insecure file link; it was not downloaded.")
        resp = http_request("GET", url, headers=self.provider.auth_headers(self.connection.api_token or ""),
                            allow_redirects=False)
        return _ok(resp, "the file download").content


def _ok(resp, what):
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            detail = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("error") or body.get("message") or ""
        except Exception:
            pass
        raise ConnectorError(f"The e-signature service refused {what} ({resp.status_code})" + (f": {str(detail)[:200]}" if detail else "."))
    return resp


def signature_page(pdf_bytes: bytes, signers: list, *, tag=None):
    """The contract with a "Signatures" page appended: one box per signer.
    Returns (pdf_bytes, boxes) where boxes[i] = {"page": 1-based page, "x", "y", "w", "h"} as fractions of the page
    (from the top-left) for signer i. `tag(i, signer)` optionally writes a text tag into the box (DocuSeal)."""
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        base_pages = len(reader.pages)
    except Exception:
        raise ConnectorError("The contract document is not a readable PDF.")
    if reader.is_encrypted:
        raise ConnectorError("The contract PDF is password-protected; unlock it before sending for signature.")
    width, height = A4
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    boxes = []
    margin, box_h, gap, top = 56, 70, 14, 110
    box_w = width - 2 * margin
    for i, s in enumerate(signers):
        slot = i % BOXES_PER_PAGE
        if slot == 0:
            if i:
                c.showPage()
            c.setFont("Helvetica-Bold", 15)
            c.drawString(margin, height - 64, SIG_PAGE_TITLE)
            c.setFont("Helvetica", 9)
            c.drawString(margin, height - 80, "Each signer signs in their own box. This page forms part of the document above it.")
        y_top = height - top - slot * (box_h + gap)
        c.setLineWidth(0.6)
        c.rect(margin, y_top - box_h, box_w, box_h)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(margin + 8, y_top - 14, f"{i + 1}. {s['name']}")
        c.setFont("Helvetica", 8.5)
        c.drawString(margin + 8, y_top - 26, s["email"])
        if tag is not None:
            c.setFont("Helvetica", 7)
            c.drawString(margin + 200, y_top - box_h + 12, tag(i, s))
        page = base_pages + 1 + i // BOXES_PER_PAGE
        # The signature area: right part of the box.
        sx, sy, sw, sh = margin + 200, y_top - box_h + 8, box_w - 210, box_h - 16
        boxes.append({"page": page, "x": round(sx / width, 4), "y": round((height - (sy + sh)) / height, 4),
                      "w": round(sw / width, 4), "h": round(sh / height, 4)})
    c.save()
    writer = PdfWriter()
    for p in reader.pages:
        writer.add_page(p)
    for p in PdfReader(io.BytesIO(buf.getvalue())).pages:
        writer.add_page(p)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), boxes


class OpenESignProvider(ESignProvider):
    """An e-signature service connected per organisation with an API token (no OAuth)."""
    kind = "org"
    org_level = True
    file_hosts: tuple = ()
    max_pdf_bytes = 25 * 1024 * 1024

    def auth_headers(self, token: str) -> dict:
        raise NotImplementedError

    def check(self, api) -> str:
        """A harmless authenticated call; returns a short description of the account."""
        raise NotImplementedError

    def webhook_ok(self, connection, request) -> bool:
        """Whether a callback really came from the service. The callback address already carries an unguessable
        token; providers that sign their callbacks are verified as well."""
        return True

    def webhook_envelope(self, payload: dict) -> str | None:
        raise NotImplementedError


# ── DocuSeal ─────────────────────────────────────────────────────────────────────────────────────────────────────
class DocuSeal(OpenESignProvider):
    key, name = "docuseal", "DocuSeal"
    file_hosts = ("docuseal.com", "docuseal.eu", "docuseal.co")
    STATUS = {"completed": "completed", "declined": "declined", "expired": "voided", "archived": "voided"}

    def auth_headers(self, token):
        return {"X-Auth-Token": token, "Accept": "application/json"}

    def check(self, api):
        resp = api.request("GET", "/submissions", params={"limit": 1})
        _ok(resp, "the connection test")
        return "DocuSeal"

    def send(self, api, *, name, filename, pdf_bytes, signers, message, callback_url):
        roles = [f"Signer {i + 1}" for i in range(len(signers))]
        pdf, _ = signature_page(pdf_bytes, signers, tag=lambda i, s: "{{Signature;role=%s;type=signature}}" % roles[i])
        body = {
            "name": name[:255], "send_email": True, "order": "preserved",
            "documents": [{"name": filename[:255], "file": base64.b64encode(pdf).decode()}],
            "submitters": [{"name": s["name"], "email": s["email"], "role": roles[i], "order": int(s.get("order", i + 1)) - 1}
                           for i, s in enumerate(signers)],
        }
        if message:
            body["message"] = {"subject": f"Please sign: {name}"[:200], "body": message[:2000]}
        resp = api.request("POST", "/submissions/pdf", json=body)
        if resp.status_code == 404:
            raise ConnectorError("This DocuSeal installation cannot send PDFs through its API (that needs DocuSeal Pro "
                                 "or DocuSeal cloud). Choose another e-signature service or upgrade DocuSeal.")
        data = _ok(resp, "the submission").json()
        sid = data.get("id") if isinstance(data, dict) else None
        if sid is None and isinstance(data, list) and data:
            sid = data[0].get("submission_id")
        if sid is None:
            raise ConnectorError("DocuSeal did not return a submission number.")
        return str(sid)

    def _get(self, api, envelope_id):
        return _ok(api.request("GET", f"/submissions/{envelope_id}"), "the status request").json()

    def status(self, api, envelope_id):
        data = self._get(api, envelope_id)
        subs = data.get("submitters") or []
        signers = [{"email": (s.get("email") or "").lower(), "status": s.get("status", ""), "signed_at": s.get("completed_at")}
                   for s in subs]
        if data.get("archived_at"):
            state = "voided"
        elif any(s.get("declined_at") for s in subs):
            state = "declined"
        else:
            state = self.STATUS.get(str(data.get("status", "")).lower())
            if state is None:
                state = "delivered" if any(s.get("opened_at") for s in subs) else "sent"
        return {"status": state, "signers": signers}

    def signed_pdf(self, api, envelope_id):
        data = self._get(api, envelope_id)
        url = data.get("combined_document_url") or next((d.get("url") for d in data.get("documents") or [] if d.get("url")), None)
        if not url:
            raise ConnectorError("DocuSeal has not produced the signed document yet.")
        return api.fetch_file(url)

    def certificate(self, api, envelope_id):
        url = self._get(api, envelope_id).get("audit_log_url")
        return api.fetch_file(url) if url else None

    def void(self, api, envelope_id, reason):
        _ok(api.request("DELETE", f"/submissions/{envelope_id}"), "archiving the submission")

    def webhook_envelope(self, payload):
        data = payload.get("data") or {}
        return str(data.get("submission_id") or (data.get("submission") or {}).get("id") or data.get("id") or "") or None


# ── Documenso ────────────────────────────────────────────────────────────────────────────────────────────────────
class Documenso(OpenESignProvider):
    key, name = "documenso", "Documenso"
    STATUS = {"COMPLETED": "completed", "REJECTED": "declined", "CANCELLED": "voided"}

    def auth_headers(self, token):
        return {"Authorization": token, "Accept": "application/json"}

    def check(self, api):
        _ok(api.request("GET", "/envelope", params={"perPage": 1}), "the connection test")
        return "Documenso"

    def send(self, api, *, name, filename, pdf_bytes, signers, message, callback_url):
        pdf, boxes = signature_page(pdf_bytes, signers)
        orders = [int(s.get("order", i + 1)) for i, s in enumerate(signers)]
        payload = {
            "type": "DOCUMENT", "title": name[:255],
            "meta": {"subject": f"Please sign: {name}"[:200], "message": (message or "")[:2000],
                     "signingOrder": "SEQUENTIAL" if len(set(orders)) > 1 else "PARALLEL"},
            "recipients": [{
                "email": s["email"], "name": s["name"], "role": "SIGNER", "signingOrder": orders[i],
                "fields": [{"identifier": 0, "type": "SIGNATURE", "page": boxes[i]["page"],
                            "positionX": round(boxes[i]["x"] * 100, 2), "positionY": round(boxes[i]["y"] * 100, 2),
                            "width": round(boxes[i]["w"] * 100, 2), "height": round(boxes[i]["h"] * 100, 2)}],
            } for i, s in enumerate(signers)],
        }
        created = _ok(api.request("POST", "/envelope/create", data={"payload": json.dumps(payload)},
                                  files={"files": (filename if filename.lower().endswith(".pdf") else filename + ".pdf", pdf,
                                                   "application/pdf")}), "the envelope").json()
        envelope = created.get("id")
        if not envelope:
            raise ConnectorError("Documenso did not return an envelope id.")
        _ok(api.request("POST", "/envelope/distribute", json={"envelopeId": envelope}), "sending the envelope")
        return str(envelope)

    def _get(self, api, envelope_id):
        return _ok(api.request("GET", f"/envelope/{envelope_id}"), "the status request").json()

    def status(self, api, envelope_id):
        data = self._get(api, envelope_id)
        recipients = data.get("recipients") or []
        signers = [{"email": (r.get("email") or "").lower(), "status": r.get("signingStatus", ""), "signed_at": r.get("signedAt")}
                   for r in recipients]
        state = self.STATUS.get(str(data.get("status", "")).upper())
        if state is None:
            state = "delivered" if any(r.get("readStatus") == "OPENED" for r in recipients) else "sent"
        return {"status": state, "signers": signers}

    def signed_pdf(self, api, envelope_id):
        items = self._get(api, envelope_id).get("envelopeItems") or []
        if not items:
            raise ConnectorError("Documenso returned no document for this envelope.")
        resp = _ok(api.request("GET", f"/envelope/item/{items[0]['id']}/download", params={"version": "signed"}),
                   "the signed document")
        if "json" in (resp.headers.get("content-type") or ""):
            url = (resp.json() or {}).get("downloadUrl") or (resp.json() or {}).get("url")
            if not url:
                raise ConnectorError("Documenso returned no download address.")
            return api.fetch_file(url)
        return resp.content

    def certificate(self, api, envelope_id):
        return None          # Documenso appends its signing certificate to the signed PDF

    def void(self, api, envelope_id, reason):
        _ok(api.request("POST", "/envelope/cancel", json={"envelopeId": envelope_id, "reason": reason[:200]}), "cancelling")

    def webhook_ok(self, connection, request):
        secret = connection.webhook_secret or ""
        if not secret:
            return True              # the unguessable callback address is the protection
        return hmac.compare_digest(request.META.get("HTTP_X_DOCUMENSO_SECRET", ""), secret)

    def webhook_envelope(self, payload):
        data = payload.get("payload") or {}
        return str(data.get("envelopeId") or "") or None


# ── SignuDoc ─────────────────────────────────────────────────────────────────────────────────────────────────────
class SignuDoc(OpenESignProvider):
    key, name = "signudoc", "SignuDoc"
    max_pdf_bytes = int(4.5 * 1024 * 1024)       # larger files need SignuDoc's separate upload step
    STATUS = {"completed": "completed", "declined": "declined", "voided": "voided"}

    def auth_headers(self, token):
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def check(self, api):
        data = _ok(api.request("GET", "/me"), "the connection test").json()
        return str(((data.get("user") or {}).get("email")) or "SignuDoc")

    def send(self, api, *, name, filename, pdf_bytes, signers, message, callback_url):
        pdf, boxes = signature_page(pdf_bytes, signers)
        if len(pdf) > self.max_pdf_bytes:
            raise ConnectorError("SignuDoc accepts documents up to 4.5 MB through this connection.")
        data = {
            "title": name[:200],
            "signers": [{"ref": f"s{i + 1}", "name": s["name"], "email": s["email"], "step": int(s.get("order", i + 1))}
                        for i, s in enumerate(signers)],
            "fields": [{"signer": f"s{i + 1}", "type": "signature", "page": boxes[i]["page"], "x": boxes[i]["x"],
                        "y": boxes[i]["y"], "width": boxes[i]["w"], "height": boxes[i]["h"]} for i in range(len(signers))],
            "send": {"delivery": "email", "expiresInDays": 30, "permittedUseConfirmed": True},
        }
        idem = hashlib.sha256((name + "|" + ",".join(s["email"] for s in signers)).encode() + pdf[:4096]).hexdigest()[:40]
        created = _ok(api.request("POST", "/envelopes", data={"data": json.dumps(data)},
                                  files={"file": (filename if filename.lower().endswith(".pdf") else filename + ".pdf", pdf,
                                                  "application/pdf")},
                                  headers={"Idempotency-Key": idem}), "the envelope").json()
        envelope = created.get("id") or (created.get("envelope") or {}).get("id")
        if not envelope:
            raise ConnectorError("SignuDoc did not return an envelope id.")
        return str(envelope)

    def status(self, api, envelope_id):
        data = _ok(api.request("GET", f"/envelopes/{envelope_id}"), "the status request").json()
        signers = [{"email": (s.get("email") or "").lower(), "status": s.get("status", ""), "signed_at": s.get("signedAt")}
                   for s in data.get("signers") or []]
        if data.get("expired"):
            state = "voided"
        else:
            state = self.STATUS.get(str(data.get("status", "")).lower())
            if state is None:
                state = "delivered" if any(s.get("viewedAt") for s in data.get("signers") or []) else "sent"
        return {"status": state, "signers": signers}

    def signed_pdf(self, api, envelope_id):
        return _ok(api.request("GET", f"/envelopes/{envelope_id}/files/signed"), "the signed document").content

    def certificate(self, api, envelope_id):
        return _ok(api.request("GET", f"/envelopes/{envelope_id}/files/certificate"), "the certificate").content

    def void(self, api, envelope_id, reason):
        _ok(api.request("POST", f"/envelopes/{envelope_id}/void", json={"reason": reason[:500]}), "voiding")

    def webhook_ok(self, connection, request):
        """Standard Webhooks: v1,<base64 HMAC-SHA256 of "id.timestamp.body"> keyed with the whsec_ secret."""
        secret = connection.webhook_secret or ""
        if not secret:
            return True
        return standard_webhook_ok(secret, request.META.get("HTTP_WEBHOOK_ID", ""), request.META.get("HTTP_WEBHOOK_TIMESTAMP", ""),
                                   request.META.get("HTTP_WEBHOOK_SIGNATURE", ""), request.body)

    def webhook_envelope(self, payload):
        data = payload.get("data") or {}
        return str((data.get("envelope") or {}).get("id") or "") or None


def standard_webhook_ok(secret: str, msg_id: str, timestamp: str, signatures: str, body: bytes, tolerance: int = 300) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if not msg_id or abs(time.time() - ts) > tolerance:
        return False
    try:
        key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    except Exception:
        return False
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    for sig in (signatures or "").split():
        version, _, value = sig.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return True
    return False


ORG_PROVIDERS: dict[str, OpenESignProvider] = {p.key: p for p in (DocuSeal(), Documenso(), SignuDoc())}
