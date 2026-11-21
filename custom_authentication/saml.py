"""
Single sign-on with SAML 2.0 (Microsoft Entra ID, Okta, ADFS, Google Workspace, PingFederate, ...).
The OpenID Connect flow is in sso.py; both end in the same one-time code exchange as password sign-in.

    GET  /api/v1/auth/sso/saml/metadata/   our service provider metadata (entity ID, ACS address)
    POST /api/v1/auth/sso/saml/acs/        the identity provider posts SAMLResponse + RelayState here

The flow starts at POST /api/v1/auth/sso/start/ (sso.SSOStartView), which sends the browser to the identity
provider with a SAML AuthnRequest (HTTP-Redirect binding) and a single-use RelayState (10 minutes).

Security (each check is tested in tests_saml.py):
* Only service-provider-initiated sign-in: the assertion must answer our request (InResponseTo) and the
  RelayState is single use, so an unsolicited or replayed response is refused.
* XML is parsed without DTDs, entities or network access; responses over 512 KB are refused.
* The signature is verified with signxml against the certificate(s) the administrator configured (never a
  certificate inside the message), with SHA-2 algorithms only. The signature must be a direct child of the
  element it signs, with exactly one reference, and everything we read comes from the verified output that
  signxml returns, never from the original document (defeats signature-wrapping).
* Assertion checks: issuer is the configured identity provider; audience is our entity ID; NotBefore /
  NotOnOrAfter (2 minutes' clock skew); bearer confirmation with our ACS address as Recipient; each assertion
  ID accepted once.
* Identity values containing comments or child elements are refused (comment-truncation attacks).
* Encrypted assertions are refused with a clear message (signed, TLS-protected assertions are required).
* The email then goes through the same rules as OpenID Connect (sso.user_for): it must be in the
  organisation's domains and an existing account must belong to that organisation.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import secrets
import zlib
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlencode
from xml.sax.saxutils import escape, quoteattr

from django.core.cache import cache
from django.http import HttpResponse
from lxml import etree
from rest_framework.parsers import FormParser
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from .sso import SSOError, _back, _throttles, user_for

logger = logging.getLogger(__name__)

NS = {"samlp": "urn:oasis:names:tc:SAML:2.0:protocol", "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
      "ds": "http://www.w3.org/2000/09/xmldsig#", "md": "urn:oasis:names:tc:SAML:2.0:metadata"}
SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"
BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"
EMAIL_FORMAT = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
MAX_RESPONSE_BYTES = 512 * 1024
CLOCK_SKEW = timedelta(minutes=2)
EMAIL_ATTRIBUTES = (
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",   # Entra ID, ADFS
    "urn:oid:0.9.2342.19200300.100.1.3",                                     # mail (OID form)
    "email", "mail", "emailaddress", "Email", "EmailAddress",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn",
)
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


# ── addresses ─────────────────────────────────────────────────────────────────────────────────
def api_base(request) -> str:
    base = (os.getenv("SSO_PUBLIC_API_URL") or os.getenv("INTEGRATIONS_PUBLIC_API_URL") or "").strip().rstrip("/")
    return base or request.build_absolute_uri("/").rstrip("/")


def sp_entity_id(request) -> str:
    return f"{api_base(request)}/api/v1/auth/sso/saml/metadata/"


def acs_url(request) -> str:
    return f"{api_base(request)}/api/v1/auth/sso/saml/acs/"


# ── certificates and identity provider metadata ──────────────────────────────────────────────
PEM_RE = re.compile(r"-----BEGIN CERTIFICATE-----\s*(.+?)\s*-----END CERTIFICATE-----", re.S)


def normalise_certificates(text: str) -> str:
    """One or more certificates (PEM, or bare base64 as in metadata) -> canonical PEM blocks. Raises SSOError."""
    from cryptography import x509
    text = (text or "").strip()
    bodies = PEM_RE.findall(text) or ([text] if text else [])
    pems = []
    for body in bodies:
        b64 = re.sub(r"\s+", "", body)
        try:
            der = base64.b64decode(b64, validate=True)
            x509.load_der_x509_certificate(der)
        except Exception as exc:
            raise SSOError("The identity provider certificate could not be read. Paste the signing certificate (PEM or base64).") from exc
        lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
        pems.append("-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----")
    return "\n".join(pems)


def certificates(cfg) -> list[str]:
    return ["-----BEGIN CERTIFICATE-----\n" + b + "\n-----END CERTIFICATE-----"
            for b in PEM_RE.findall(cfg.saml_idp_certificates or "")]


def certificate_summary(cfg) -> list[dict]:
    from cryptography import x509
    out = []
    for pem in certificates(cfg):
        try:
            c = x509.load_pem_x509_certificate(pem.encode())
            out.append({"subject": c.subject.rfc4514_string(), "expires": c.not_valid_after_utc.isoformat(),
                        "expired": c.not_valid_after_utc < datetime.now(dt_timezone.utc)})
        except Exception:
            continue
    return out


def _parse(xml: bytes):
    if re.search(rb"<!(DOCTYPE|ENTITY)", xml, re.I):
        raise SSOError("The identity provider's message is not acceptable.")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False,
                             huge_tree=False, remove_comments=False, remove_pis=True)
    try:
        return etree.fromstring(xml, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise SSOError("The identity provider's message could not be read.") from exc


def parse_idp_metadata(xml_text: str) -> dict:
    """{entity_id, sso_url, certificates} from identity provider metadata XML. Raises SSOError."""
    raw = (xml_text or "").encode("utf-8")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise SSOError("The metadata file is too large.")
    root = _parse(raw)
    descriptor = root if etree.QName(root).localname == "EntityDescriptor" else root.find(".//md:EntityDescriptor", NS)
    if descriptor is None or not descriptor.get("entityID"):
        raise SSOError("This is not SAML identity provider metadata (no EntityDescriptor).")
    idp = descriptor.find("md:IDPSSODescriptor", NS)
    if idp is None:
        raise SSOError("The metadata has no identity provider section (IDPSSODescriptor).")
    sso_url = next((s.get("Location") for s in idp.findall("md:SingleSignOnService", NS)
                    if s.get("Binding") == "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"), None)
    if not sso_url or not sso_url.startswith("https://"):
        raise SSOError("The metadata has no https sign-in address for the HTTP-Redirect binding.")
    certs = [_text(c) for kd in idp.findall("md:KeyDescriptor", NS) if kd.get("use") in (None, "signing")
             for c in kd.findall(".//ds:X509Certificate", NS)]
    if not certs:
        raise SSOError("The metadata has no signing certificate.")
    return {"entity_id": descriptor.get("entityID"), "sso_url": sso_url,
            "certificates": normalise_certificates("\n".join(
                "-----BEGIN CERTIFICATE-----\n" + c + "\n-----END CERTIFICATE-----" for c in certs if c))}


def sp_metadata(request) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<md:EntityDescriptor xmlns:md="{NS["md"]}" entityID={quoteattr(sp_entity_id(request))}>'
            f'<md:SPSSODescriptor AuthnRequestsSigned="false" WantAssertionsSigned="true" '
            f'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
            f'<md:NameIDFormat>{EMAIL_FORMAT}</md:NameIDFormat>'
            f'<md:AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
            f'Location={quoteattr(acs_url(request))} index="0" isDefault="true"/>'
            f'</md:SPSSODescriptor></md:EntityDescriptor>')


def check_config(cfg) -> None:
    if not (cfg.saml_idp_entity_id and cfg.saml_idp_sso_url.startswith("https://") and certificates(cfg)):
        raise SSOError("Fill in the identity provider's entity ID, https sign-in address and signing certificate.")


# ── the request ──────────────────────────────────────────────────────────────────────────────
def _instant(dt=None) -> str:
    return (dt or datetime.now(dt_timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def authn_request_url(cfg, request, relay_state: str, login_hint: str = "") -> tuple[str, str]:
    """(request ID, the identity provider URL with a deflated AuthnRequest and RelayState)."""
    check_config(cfg)
    request_id = "_" + secrets.token_hex(20)
    subject = (f'<saml:Subject><saml:NameID Format="{EMAIL_FORMAT}">{escape(login_hint)}</saml:NameID></saml:Subject>'
               if login_hint and EMAIL_RE.fullmatch(login_hint) and cfg.saml_send_login_hint else "")
    xml = (f'<samlp:AuthnRequest xmlns:samlp="{NS["samlp"]}" xmlns:saml="{NS["saml"]}" ID="{request_id}" Version="2.0" '
           f'IssueInstant="{_instant()}" Destination={quoteattr(cfg.saml_idp_sso_url)} '
           f'AssertionConsumerServiceURL={quoteattr(acs_url(request))} '
           f'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">'
           f'<saml:Issuer>{escape(sp_entity_id(request))}</saml:Issuer>{subject}'
           f'<samlp:NameIDPolicy Format="{EMAIL_FORMAT}" AllowCreate="true"/></samlp:AuthnRequest>')
    deflated = zlib.compress(xml.encode("utf-8"))[2:-4]          # raw DEFLATE (HTTP-Redirect binding)
    query = urlencode({"SAMLRequest": base64.b64encode(deflated).decode(), "RelayState": relay_state})
    url = cfg.saml_idp_sso_url
    return request_id, f"{url}{'&' if '?' in url else '?'}{query}"


# ── the response ─────────────────────────────────────────────────────────────────────────────
def _text(el) -> str:
    """Text of a leaf element. Comments, processing instructions or child elements inside it are refused."""
    if el is None:
        return ""
    if len(el):
        raise SSOError("The identity provider's answer is not acceptable.")
    return (el.text or "").strip()


def _time(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt_timezone.utc)
    except ValueError as exc:
        raise SSOError("The identity provider's answer has an invalid time.") from exc


def _verified(element, certs):
    """The element as signxml verified it (re-parsed from the signed bytes), or None when it carries no
    signature of its own. Raises SSOError when a signature is present but does not verify."""
    if element.find("ds:Signature", NS) is None:
        return None
    from signxml import SignatureMethod, XMLVerifier
    from signxml.verifier import SignatureConfiguration
    # Public-key SHA-2/SHA-3 signatures only: no SHA-1, no HMAC (a shared secret), no DSA.
    methods = frozenset(m for m in SignatureMethod if "SHA1" not in m.name and not m.name.startswith(("HMAC", "DSA")))
    config = SignatureConfiguration(location="./", expect_references=1, signature_methods=methods,
                                    ignore_ambiguous_key_info=True)
    data = etree.tostring(element)
    last = None
    for cert in certs:
        try:
            result = XMLVerifier().verify(data, x509_cert=cert, expect_config=config)
            return result.signed_xml
        except Exception as exc:       # wrong certificate (rollover) or a bad signature: try the next one
            last = exc
    logger.warning("SAML signature did not verify: %s", last)
    raise SSOError("The identity provider's signature could not be verified. Check the signing certificate.")


def email_from_response(cfg, request, saml_response: str, request_id: str) -> str:
    try:
        raw = base64.b64decode(saml_response or "", validate=False)
    except Exception as exc:
        raise SSOError("The identity provider's answer could not be read.") from exc
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise SSOError("The identity provider's answer could not be read.")
    root = _parse(raw)
    if etree.QName(root).text != f"{{{NS['samlp']}}}Response":
        raise SSOError("The identity provider's answer is not a SAML response.")
    status = root.find("samlp:Status/samlp:StatusCode", NS)
    if status is None or status.get("Value") != SUCCESS:
        raise SSOError("Your identity provider refused the sign-in.")
    if root.find("saml:EncryptedAssertion", NS) is not None:
        raise SSOError("Encrypted assertions are not supported. Turn off assertion encryption for this app at your "
                       "identity provider (the assertion is still signed and sent over TLS).")
    destination = root.get("Destination")
    if destination and destination != acs_url(request):
        raise SSOError("The identity provider's answer was meant for another address.")

    certs = certificates(cfg)
    if not certs:
        raise SSOError("No signing certificate is configured for your identity provider.")
    assertions = root.findall("saml:Assertion", NS)
    if len(assertions) != 1:
        raise SSOError("The identity provider's answer must contain exactly one assertion.")
    # Prefer the assertion's own signature; otherwise the whole response must be signed.
    assertion = _verified(assertions[0], certs)
    if assertion is None:
        signed_response = _verified(root, certs)
        if signed_response is None:
            raise SSOError("The identity provider's answer is not signed.")
        signed = signed_response.findall("saml:Assertion", NS)
        if len(signed) != 1:
            raise SSOError("The identity provider's answer must contain exactly one assertion.")
        assertion = signed[0]
    return _check_assertion(cfg, request, assertion, request_id)


def _check_assertion(cfg, request, assertion, request_id) -> str:
    now = datetime.now(dt_timezone.utc)
    if _text(assertion.find("saml:Issuer", NS)) != cfg.saml_idp_entity_id:
        raise SSOError("The answer came from a different identity provider than the one configured.")
    conditions = assertion.find("saml:Conditions", NS)
    if conditions is None:
        raise SSOError("The identity provider's answer has no conditions.")
    not_before, not_after = _time(conditions.get("NotBefore")), _time(conditions.get("NotOnOrAfter"))
    if (not_before and now + CLOCK_SKEW < not_before) or (not_after and now - CLOCK_SKEW >= not_after):
        raise SSOError("The sign-in answer has expired. Please start again.")
    audiences = [_text(a) for a in conditions.findall("saml:AudienceRestriction/saml:Audience", NS)]
    if sp_entity_id(request) not in audiences:
        raise SSOError("The identity provider's answer is for a different application (audience). "
                       "Use this service's entity ID at your identity provider.")
    subject = assertion.find("saml:Subject", NS)
    if subject is None:
        raise SSOError("The identity provider's answer has no subject.")
    confirmed = False
    for sc in subject.findall("saml:SubjectConfirmation", NS):
        data = sc.find("saml:SubjectConfirmationData", NS)
        if sc.get("Method") != BEARER or data is None:
            continue
        expires = _time(data.get("NotOnOrAfter"))
        if (data.get("Recipient") == acs_url(request) and data.get("InResponseTo") == request_id
                and expires is not None and now - CLOCK_SKEW < expires):
            confirmed = True
            break
    if not confirmed:
        raise SSOError("The sign-in answer did not match this request. Please start again.")
    assertion_id = assertion.get("ID") or ""
    if not assertion_id or not cache.add(f"saml-assertion:{cfg.client_id}:{assertion_id}", 1, timeout=24 * 3600):
        raise SSOError("This sign-in answer was already used. Please start again.")
    return _email(cfg, assertion, subject)


def _email(cfg, assertion, subject) -> str:
    wanted = [cfg.saml_email_attribute] if cfg.saml_email_attribute else list(EMAIL_ATTRIBUTES)
    by_name = {}
    for attr in assertion.findall("saml:AttributeStatement/saml:Attribute", NS):
        values = [_text(v) for v in attr.findall("saml:AttributeValue", NS)]
        if values:
            by_name.setdefault(attr.get("Name") or "", values[0])
    candidates = [by_name.get(name, "") for name in wanted]
    if not cfg.saml_email_attribute:
        candidates.append(_text(subject.find("saml:NameID", NS)))
    for value in candidates:
        email = (value or "").strip().lower()
        if EMAIL_RE.fullmatch(email):
            return email
    raise SSOError("Your identity provider did not send an email address. Ask your administrator to release the "
                   "email attribute (or use the email as NameID).")


# ── endpoints ────────────────────────────────────────────────────────────────────────────────
class SAMLMetadataView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return HttpResponse(sp_metadata(request), content_type="application/samlmetadata+xml; charset=utf-8")


class SAMLACSView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = [FormParser]

    def get_throttles(self):
        return [t() for t in _throttles()]

    def post(self, request):
        from django.conf import settings
        from .models import OrganisationSSO
        state = str(request.data.get("RelayState") or "")
        saved = cache.get(f"sso-state:{state}") if state else None
        if saved:
            cache.delete(f"sso-state:{state}")   # single use
        if not saved or saved.get("protocol") != "saml":
            return _see_other(_back(settings.SPA_REDIRECT_URIS[0] if settings.SPA_REDIRECT_URIS else "/",
                                    error="This sign-in link has expired. Please start again."))
        redirect_uri = saved["redirect_uri"]
        try:
            cfg = OrganisationSSO.objects.select_related("client").get(client_id=saved["org"], enabled=True, protocol="saml")
            email = email_from_response(cfg, request, str(request.data.get("SAMLResponse") or ""), saved["request_id"])
            user = user_for(cfg, email)
        except OrganisationSSO.DoesNotExist:
            return _see_other(_back(redirect_uri, error="Single sign-on is no longer enabled for your organisation."))
        except SSOError as exc:
            logger.info("SAML sign-in refused for organisation %s: %s", saved.get("org"), exc)
            return _see_other(_back(redirect_uri, error=str(exc)))
        except Exception:
            logger.exception("SAML ACS failed")
            return _see_other(_back(redirect_uri, error="Sign-in failed. Please try again."))
        from .browser_auth import issue_browser_code
        from .views import _login_succeeded
        _login_succeeded(user.email)
        grant = issue_browser_code(user, saved["challenge"], redirect_uri)
        logger.info("SAML sign-in: user %s (organisation %s)", user.pk, cfg.client_id)
        return _see_other(_back(redirect_uri, code=grant["code"], client_id=grant["client_id"], sso="1"))


def _see_other(response):
    response.status_code = 303      # the identity provider POSTed here; the app is opened with GET
    return response
