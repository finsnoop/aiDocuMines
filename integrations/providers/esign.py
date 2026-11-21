"""
E-signature: DocuSign and Adobe Acrobat Sign.

Both are connected per person under Integrations (OAuth, tokens encrypted at rest). Sending, status, the signed
PDF, the certificate and voiding all go through the person's own connected account. Webhooks are only a hint
to check: the status is always re-read from the provider's API (contracts/esign.py), so a forged callback
cannot mark anything signed.

Settings (environment):
  DOCUSIGN_CLIENT_ID, DOCUSIGN_CLIENT_SECRET, DOCUSIGN_AUTH_BASE (default https://account-d.docusign.com, the
  developer sandbox; production https://account.docusign.com), DOCUSIGN_CONNECT_HMAC_KEY (Connect HMAC secret)
  ADOBE_SIGN_CLIENT_ID, ADOBE_SIGN_CLIENT_SECRET, ADOBE_SIGN_SHARD (default na1)
"""
from __future__ import annotations

import base64
import os

from .base import ConnectorError, Provider, http_request


class ESignProvider(Provider):
    category = "E-signature"
    can_import = False
    can_export = False
    pkce = False

    # Operations used by contracts/esign.py ────────────────────────────────────────────────────
    def send(self, api, *, name, filename, pdf_bytes, signers, message, callback_url) -> str:
        raise NotImplementedError

    def status(self, api, envelope_id) -> dict:
        """{"status": sent|delivered|completed|declined|voided, "signers": [{email, status, signed_at}]}."""
        raise NotImplementedError

    def signed_pdf(self, api, envelope_id) -> bytes:
        raise NotImplementedError

    def certificate(self, api, envelope_id) -> bytes | None:
        return None

    def void(self, api, envelope_id, reason) -> None:
        raise NotImplementedError


def _ok(resp, what):
    if resp.status_code >= 400:
        raise ConnectorError(f"The e-signature service refused {what} ({resp.status_code}).")
    return resp


class DocuSign(ESignProvider):
    key, name = "docusign", "DocuSign"
    scopes = ["signature", "extended"]
    client_id_env, client_secret_env = "DOCUSIGN_CLIENT_ID", "DOCUSIGN_CLIENT_SECRET"
    token_auth = "basic"
    STATUS = {"sent": "sent", "delivered": "delivered", "completed": "completed", "declined": "declined",
              "voided": "voided", "created": "sent"}

    @property
    def auth_base(self):
        return os.getenv("DOCUSIGN_AUTH_BASE", "https://account-d.docusign.com").rstrip("/")

    @property
    def authorize_url(self):
        return f"{self.auth_base}/oauth/auth"

    def token_endpoint(self, extra=None):
        return f"{self.auth_base}/oauth/token"

    def after_connect(self, api, tokens):
        info = _ok(http_request("GET", f"{self.auth_base}/oauth/userinfo", headers=api.headers()), "the account lookup").json()
        accounts = info.get("accounts") or []
        default = next((a for a in accounts if a.get("is_default")), accounts[0] if accounts else None)
        if not default:
            raise ConnectorError("This DocuSign user has no account.")
        return {"account_id": default["account_id"], "base_uri": default["base_uri"].rstrip("/"), "email": info.get("email", "")}

    def account(self, api):
        return (api.account.extra or {}).get("email", "") or "DocuSign"

    def _base(self, api):
        extra = api.account.extra or {}
        if not extra.get("base_uri") or not extra.get("account_id"):
            raise ConnectorError("Connect DocuSign again to finish setting it up.")
        return f"{extra['base_uri']}/restapi/v2.1/accounts/{extra['account_id']}"

    def send(self, api, *, name, filename, pdf_bytes, signers, message, callback_url):
        body = {
            "emailSubject": f"Please sign: {name}"[:100], "emailBlurb": (message or "")[:2000], "status": "sent",
            "documents": [{"documentBase64": base64.b64encode(pdf_bytes).decode(), "name": filename[:100],
                           "fileExtension": filename.rsplit(".", 1)[-1].lower() if "." in filename else "pdf", "documentId": "1"}],
            "recipients": {"signers": [{"email": s["email"], "name": s["name"], "recipientId": str(i + 1),
                                        "routingOrder": str(s.get("order", i + 1))} for i, s in enumerate(signers)]},
        }
        if callback_url:
            body["eventNotification"] = {"url": callback_url, "requireAcknowledgment": "true", "includeHMAC": "true",
                                         "envelopeEvents": [{"envelopeEventStatusCode": c} for c in ("completed", "declined", "voided")],
                                         "recipientEvents": [{"recipientEventStatusCode": c} for c in ("Completed", "Declined")]}
        data = _ok(api.request("POST", f"{self._base(api)}/envelopes", json=body), "the envelope").json()
        return data["envelopeId"]

    def status(self, api, envelope_id):
        env = _ok(api.request("GET", f"{self._base(api)}/envelopes/{envelope_id}"), "the status request").json()
        rec = _ok(api.request("GET", f"{self._base(api)}/envelopes/{envelope_id}/recipients"), "the recipients request").json()
        signers = [{"email": s.get("email", ""), "status": s.get("status", ""), "signed_at": s.get("signedDateTime")}
                   for s in rec.get("signers", [])]
        return {"status": self.STATUS.get(str(env.get("status", "")).lower(), "sent"), "signers": signers}

    def signed_pdf(self, api, envelope_id):
        return _ok(api.request("GET", f"{self._base(api)}/envelopes/{envelope_id}/documents/combined"), "the signed document").content

    def certificate(self, api, envelope_id):
        return _ok(api.request("GET", f"{self._base(api)}/envelopes/{envelope_id}/documents/certificate"), "the certificate").content

    def void(self, api, envelope_id, reason):
        _ok(api.request("PUT", f"{self._base(api)}/envelopes/{envelope_id}", json={"status": "voided", "voidedReason": reason[:200]}), "voiding")


class AdobeSign(ESignProvider):
    key, name = "adobe_sign", "Adobe Acrobat Sign"
    scopes = ["user_login:self", "agreement_read:account", "agreement_write:account", "agreement_send:account"]
    scope_sep = "+"
    client_id_env, client_secret_env = "ADOBE_SIGN_CLIENT_ID", "ADOBE_SIGN_CLIENT_SECRET"
    STATUS = {"OUT_FOR_SIGNATURE": "sent", "OUT_FOR_APPROVAL": "sent", "WAITING_FOR_MY_SIGNATURE": "sent",
              "SIGNED": "completed", "APPROVED": "completed", "CANCELLED": "voided", "EXPIRED": "voided",
              "RECALLED": "voided", "DECLINED": "declined"}

    @property
    def shard(self):
        return os.getenv("ADOBE_SIGN_SHARD", "na1")

    @property
    def authorize_url(self):
        return f"https://secure.{self.shard}.adobesign.com/public/oauth/v2"

    def token_endpoint(self, extra=None):
        base = (extra or {}).get("api_access_point") or f"https://api.{self.shard}.adobesign.com/"
        return f"{base.rstrip('/')}/oauth/v2/token"

    def after_connect(self, api, tokens):
        return {"api_access_point": str(tokens.get("api_access_point") or f"https://api.{self.shard}.adobesign.com/").rstrip("/")}

    def account(self, api):
        return "Adobe Acrobat Sign"

    def _base(self, api):
        point = (api.account.extra or {}).get("api_access_point")
        if not point:
            raise ConnectorError("Connect Adobe Acrobat Sign again to finish setting it up.")
        return f"{point.rstrip('/')}/api/rest/v6"

    def send(self, api, *, name, filename, pdf_bytes, signers, message, callback_url):
        doc = _ok(api.request("POST", f"{self._base(api)}/transientDocuments",
                              files={"File": (filename, pdf_bytes, "application/pdf")}), "the document upload").json()
        body = {"fileInfos": [{"transientDocumentId": doc["transientDocumentId"]}], "name": name[:255],
                "message": (message or "")[:1000], "signatureType": "ESIGN", "state": "IN_PROCESS",
                "participantSetsInfo": [{"memberInfos": [{"email": s["email"]}], "order": int(s.get("order", i + 1)),
                                         "role": "SIGNER"} for i, s in enumerate(signers)]}
        return _ok(api.request("POST", f"{self._base(api)}/agreements", json=body), "the agreement").json()["id"]

    def status(self, api, envelope_id):
        data = _ok(api.request("GET", f"{self._base(api)}/agreements/{envelope_id}"), "the status request").json()
        signers = [{"email": m.get("email", ""), "status": p.get("status", ""), "signed_at": None}
                   for p in data.get("participantSetsInfo", []) for m in p.get("memberInfos", [])]
        return {"status": self.STATUS.get(str(data.get("status", "")).upper(), "sent"), "signers": signers}

    def signed_pdf(self, api, envelope_id):
        return _ok(api.request("GET", f"{self._base(api)}/agreements/{envelope_id}/combinedDocument"), "the signed document").content

    def certificate(self, api, envelope_id):
        return _ok(api.request("GET", f"{self._base(api)}/agreements/{envelope_id}/auditTrail"), "the audit trail").content

    def void(self, api, envelope_id, reason):
        _ok(api.request("PUT", f"{self._base(api)}/agreements/{envelope_id}/state",
                        json={"state": "CANCELLED", "agreementCancellationInfo": {"comment": reason[:200], "notifyOthers": True}}), "cancelling")
