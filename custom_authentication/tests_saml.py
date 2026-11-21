"""SAML 2.0 single sign-on. A test identity provider signs real assertions with signxml, so signature,
issuer, audience, timing, recipient, InResponseTo and replay checks all run for real, and the known SAML attacks
(signature wrapping, comment truncation, replay, tampering, XML entities, other organisations) are refused."""
import base64
import zlib
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.core.cache import cache
from django.test import TestCase, override_settings
from lxml import etree
from oauth2_provider.models import Grant
from rest_framework.test import APIClient

from core.tests.test_tenancy import make_user
from custom_authentication.models import Client, OrganisationSSO

SPA = "https://app.test/sign_in"
API = "https://api.test"
SP_ENTITY = f"{API}/api/v1/auth/sso/saml/metadata/"
ACS = f"{API}/api/v1/auth/sso/saml/acs/"
IDP = "https://sts.idp.test/tenant/"
CHALLENGE = "A" * 43
SAMLP, SAML = "urn:oasis:names:tc:SAML:2.0:protocol", "urn:oasis:names:tc:SAML:2.0:assertion"


def keypair(cn="idp.test"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365)).sign(key, hashes.SHA256()))
    return (key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
            cert.public_bytes(serialization.Encoding.PEM).decode())


KEY, CERT = keypair()
KEY2, CERT2 = keypair("idp-rollover.test")
EVIL_KEY, EVIL_CERT = keypair("attacker.test")


def iso(delta=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sign(element, key=KEY, cert=CERT):
    from signxml import CanonicalizationMethod, DigestAlgorithm, SignatureMethod, XMLSigner, methods
    signer = XMLSigner(method=methods.enveloped, signature_algorithm=SignatureMethod.RSA_SHA256,
                       digest_algorithm=DigestAlgorithm.SHA256, c14n_algorithm=CanonicalizationMethod.EXCLUSIVE_XML_CANONICALIZATION_1_0)
    return signer.sign(element, key=key, cert=cert, reference_uri=element.get("ID"))


def assertion_xml(email, request_id, *, issuer=IDP, audience=SP_ENTITY, recipient=ACS, aid="_a1", nameid=None,
                  not_after=300, confirm_after=300, attribute=True):
    nameid = email if nameid is None else nameid
    attr = (f'<saml:AttributeStatement><saml:Attribute Name="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress">'
            f'<saml:AttributeValue>{email}</saml:AttributeValue></saml:Attribute></saml:AttributeStatement>') if attribute else ""
    return (f'<saml:Assertion xmlns:saml="{SAML}" ID="{aid}" Version="2.0" IssueInstant="{iso()}">'
            f'<saml:Issuer>{issuer}</saml:Issuer>'
            f'<saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">{nameid}</saml:NameID>'
            f'<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
            f'<saml:SubjectConfirmationData InResponseTo="{request_id}" Recipient="{recipient}" NotOnOrAfter="{iso(confirm_after)}"/>'
            f'</saml:SubjectConfirmation></saml:Subject>'
            f'<saml:Conditions NotBefore="{iso(-60)}" NotOnOrAfter="{iso(not_after)}"><saml:AudienceRestriction>'
            f'<saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction></saml:Conditions>'
            f'{attr}</saml:Assertion>')


def response_xml(assertion, request_id, destination=ACS, status="urn:oasis:names:tc:SAML:2.0:status:Success"):
    return (f'<samlp:Response xmlns:samlp="{SAMLP}" xmlns:saml="{SAML}" ID="_r1" Version="2.0" IssueInstant="{iso()}" '
            f'Destination="{destination}" InResponseTo="{request_id}"><saml:Issuer>{IDP}</saml:Issuer>'
            f'<samlp:Status><samlp:StatusCode Value="{status}"/></samlp:Status>{assertion}</samlp:Response>')


def signed_assertion(email, request_id, key=KEY, cert=CERT, **kw):
    return etree.tostring(sign(etree.fromstring(assertion_xml(email, request_id, **kw)), key, cert)).decode()


def b64(xml):
    return base64.b64encode(xml.encode() if isinstance(xml, str) else xml).decode()


def post_form(data):
    """As identity providers do (HTTP-POST binding): an application/x-www-form-urlencoded form."""
    return APIClient().post("/api/v1/auth/sso/saml/acs/", urlencode(data), content_type="application/x-www-form-urlencoded")


@override_settings(SPA_REDIRECT_URIS=[SPA])
class SAMLTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="SAML firm")
        cls.other = Client.objects.create(name="Other SAML firm")
        cls.lawyer = make_user("lawyer@saml.test", cls.firm, groups=["Client"])
        cls.admin = make_user("admin@saml.test", cls.firm, groups=["Admin"])
        cls.stranger = make_user("intruder@saml.test", cls.other, groups=["Client"])

    def setUp(self):
        cache.clear()
        from custom_authentication.saml import normalise_certificates
        self.cfg = OrganisationSSO.objects.create(
            client=self.firm, enabled=True, protocol="saml", email_domains="saml.test", saml_idp_entity_id=IDP,
            saml_idp_sso_url="https://login.idp.test/saml2", saml_idp_certificates=normalise_certificates(CERT))
        env = mock.patch.dict("os.environ", {"SSO_PUBLIC_API_URL": API})
        env.start()
        self.addCleanup(env.stop)

    # ── helpers ──
    def start(self, email="lawyer@saml.test"):
        r = APIClient().post("/api/v1/auth/sso/start/", {"email": email, "code_challenge": CHALLENGE,
                                                        "code_challenge_method": "S256", "redirect_uri": SPA}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        url = urlparse(r.json()["authorize_url"])
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", "https://login.idp.test/saml2")
        q = parse_qs(url.query)
        request = etree.fromstring(zlib.decompress(base64.b64decode(q["SAMLRequest"][0]), -15))
        return q["RelayState"][0], request

    def acs(self, relay, saml_response):
        r = post_form({"SAMLResponse": saml_response, "RelayState": relay})
        self.assertEqual(r.status_code, 303, r.content)
        self.assertEqual(r["Cache-Control"], "no-store")
        return parse_qs(urlparse(r["Location"]).query)

    def sign_in(self, make_response, email="lawyer@saml.test"):
        relay, request = self.start(email)
        return self.acs(relay, b64(make_response(request.get("ID"))))

    def refused(self, q, words):
        self.assertNotIn("code", q, q)
        self.assertIn(words, q["error"][0])
        self.assertFalse(Grant.objects.exists())

    # ── metadata and the request ──
    def test_service_provider_metadata(self):
        r = APIClient().get("/api/v1/auth/sso/saml/metadata/")
        self.assertEqual(r.status_code, 200)
        root = etree.fromstring(r.content)
        self.assertEqual(root.get("entityID"), SP_ENTITY)
        self.assertIn(ACS.encode(), r.content)
        self.assertIn(b'WantAssertionsSigned="true"', r.content)

    def test_authn_request_names_our_acs_and_issuer(self):
        relay, request = self.start()
        self.assertEqual(request.get("AssertionConsumerServiceURL"), ACS)
        self.assertEqual(request.get("Destination"), "https://login.idp.test/saml2")
        self.assertEqual(request.find(f"{{{SAML}}}Issuer").text, SP_ENTITY)
        self.assertEqual(request.find(f"{{{SAML}}}Subject/{{{SAML}}}NameID").text, "lawyer@saml.test")
        self.assertTrue(request.get("ID").startswith("_"))

    # ── success ──
    def test_signed_assertion_signs_in_with_a_pkce_bound_code(self):
        q = self.sign_in(lambda rid: response_xml(signed_assertion("lawyer@saml.test", rid), rid))
        self.assertIn("code", q, q)
        grant = Grant.objects.get(code=q["code"][0])
        self.assertEqual((grant.user, grant.code_challenge), (self.lawyer, CHALLENGE))

    def test_signed_response_is_also_accepted(self):
        def make(rid):
            root = etree.fromstring(response_xml(assertion_xml("lawyer@saml.test", rid), rid))
            return etree.tostring(sign(root)).decode()
        q = self.sign_in(make)
        self.assertEqual(Grant.objects.get(code=q["code"][0]).user, self.lawyer)

    def test_certificate_rollover_accepts_either_configured_certificate(self):
        from custom_authentication.saml import normalise_certificates
        self.cfg.saml_idp_certificates = normalise_certificates(CERT + "\n" + CERT2)
        self.cfg.save()
        q = self.sign_in(lambda rid: response_xml(signed_assertion("lawyer@saml.test", rid, key=KEY2, cert=CERT2), rid))
        self.assertIn("code", q, q)

    # ── replay and single use ──
    def test_relay_state_and_assertion_are_single_use(self):
        relay, request = self.start()
        response = b64(response_xml(signed_assertion("lawyer@saml.test", request.get("ID")), request.get("ID")))
        self.assertIn("code", self.acs(relay, response))
        self.assertIn("expired", self.acs(relay, response)["error"][0])           # RelayState used up
        # A fresh request cannot reuse the old assertion either (wrong InResponseTo, and the ID was seen).
        relay2, _ = self.start()
        self.assertNotIn("code", self.acs(relay2, response))

    def test_assertion_id_cannot_be_replayed_even_with_a_new_request(self):
        q1 = self.sign_in(lambda rid: response_xml(signed_assertion("lawyer@saml.test", rid, aid="_same"), rid))
        self.assertIn("code", q1)
        Grant.objects.all().delete()
        q2 = self.sign_in(lambda rid: response_xml(signed_assertion("lawyer@saml.test", rid, aid="_same"), rid))
        self.refused(q2, "already used")

    def test_unsolicited_response_is_refused(self):
        r = post_form({"SAMLResponse": b64(response_xml(signed_assertion("lawyer@saml.test", "_x"), "_x")),
                       "RelayState": "made-up"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("expired", parse_qs(urlparse(r["Location"]).query)["error"][0])
        self.assertFalse(Grant.objects.exists())

    # ── signatures ──
    def test_unsigned_wrongly_signed_or_tampered_answers_are_refused(self):
        self.refused(self.sign_in(lambda rid: response_xml(assertion_xml("lawyer@saml.test", rid), rid)), "not signed")
        self.refused(self.sign_in(lambda rid: response_xml(
            signed_assertion("lawyer@saml.test", rid, key=EVIL_KEY, cert=EVIL_CERT), rid)), "signature could not be verified")
        # Valid signature, then the email is changed: the digest no longer matches.
        self.refused(self.sign_in(lambda rid: response_xml(
            signed_assertion("lawyer@saml.test", rid).replace("lawyer@saml.test", "admin@saml.test"), rid)),
            "signature could not be verified")

    def test_signature_wrapping_cannot_change_who_signs_in(self):
        """XSW: an unsigned assertion for the admin carries the lawyer's genuinely signed assertion inside it."""
        def make(rid):
            genuine = etree.fromstring(signed_assertion("lawyer@saml.test", rid, aid="_genuine"))
            sig = genuine.find("{http://www.w3.org/2000/09/xmldsig#}Signature")
            evil = etree.fromstring(assertion_xml("admin@saml.test", rid, aid="_evil"))
            evil.insert(1, sig)              # moves the genuine signature: now the evil assertion's own child
            etree.SubElement(sig, "{http://www.w3.org/2000/09/xmldsig#}Object").append(genuine)   # signed data hidden inside
            return response_xml(etree.tostring(evil).decode(), rid)
        relay, request = self.start("admin@saml.test")
        q = self.acs(relay, b64(make(request.get("ID"))))
        self.assertFalse(Grant.objects.filter(user=self.admin).exists(), q)

    def test_extra_or_missing_assertions_are_refused(self):
        def two(rid):
            a = signed_assertion("lawyer@saml.test", rid)
            return response_xml(a + a.replace('ID="_a1"', 'ID="_a2"'), rid)
        self.refused(self.sign_in(two), "exactly one assertion")
        self.refused(self.sign_in(lambda rid: response_xml("", rid)), "exactly one assertion")

    def test_comment_inside_an_identity_value_is_refused(self):
        """Comment truncation: 'admin@saml.test<!---->.evil.test' must never become admin@saml.test."""
        def make(rid):
            xml = assertion_xml("admin@saml.test.evil.test", rid, attribute=False)
            xml = xml.replace(">admin@saml.test.evil.test<", ">admin@saml.test<!---->.evil.test<")
            return response_xml(etree.tostring(sign(etree.fromstring(xml))).decode(), rid)
        relay, request = self.start("admin@saml.test")
        q = self.acs(relay, b64(make(request.get("ID"))))
        self.assertFalse(Grant.objects.filter(user=self.admin).exists(), q)
        self.assertNotIn("code", q)

    # ── assertion checks ──
    def test_issuer_audience_recipient_request_and_time_are_checked(self):
        cases = [({"issuer": "https://other-idp.test/"}, "different identity provider"),
                 ({"audience": "https://other-app.test/"}, "different application"),
                 ({"recipient": "https://evil.test/acs"}, "did not match this request"),
                 ({"not_after": -600}, "expired"),
                 ({"confirm_after": -600}, "did not match this request")]
        for kw, words in cases:
            with self.subTest(**{k: str(v) for k, v in kw.items()}):
                self.refused(self.sign_in(lambda rid: response_xml(signed_assertion("lawyer@saml.test", rid, **kw), rid)), words)
        # An assertion answering a different request.
        self.refused(self.sign_in(lambda rid: response_xml(signed_assertion("lawyer@saml.test", "_another"), rid)),
                     "did not match this request")

    def test_destination_status_and_encryption(self):
        self.refused(self.sign_in(lambda rid: response_xml(signed_assertion("lawyer@saml.test", rid), rid,
                                                            destination="https://evil.test/acs")), "another address")
        self.refused(self.sign_in(lambda rid: response_xml(signed_assertion("lawyer@saml.test", rid), rid,
                                                            status="urn:oasis:names:tc:SAML:2.0:status:Requester")), "refused")
        self.refused(self.sign_in(lambda rid: response_xml(
            f'<saml:EncryptedAssertion><xenc:EncryptedData xmlns:xenc="http://www.w3.org/2001/04/xmlenc#"/></saml:EncryptedAssertion>', rid)),
            "Encrypted assertions are not supported")

    def test_xml_entities_and_doctypes_are_refused(self):
        def make(rid):
            body = response_xml(signed_assertion("lawyer@saml.test", rid), rid)
            return '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x "lawyer@saml.test">]>' + body
        self.refused(self.sign_in(make), "not acceptable")

    def test_never_signs_in_to_another_organisation(self):
        q = self.sign_in(lambda rid: response_xml(signed_assertion("intruder@saml.test", rid), rid), "intruder@saml.test")
        self.refused(q, "different organisation")

    def test_email_must_be_in_the_organisation_domains(self):
        q = self.sign_in(lambda rid: response_xml(signed_assertion("someone@elsewhere.test", rid), rid))
        self.refused(q, "not part of your organisation's sign-in domains")

    def test_configured_email_attribute_is_required_when_set(self):
        self.cfg.saml_email_attribute = "corp_mail"
        self.cfg.save()
        self.refused(self.sign_in(lambda rid: response_xml(signed_assertion("lawyer@saml.test", rid), rid)),
                     "did not send an email address")

    def test_oidc_callback_cannot_complete_a_saml_sign_in(self):
        relay, _ = self.start()
        r = APIClient().get(f"/api/v1/auth/sso/callback/?code=x&state={relay}")
        self.assertIn("expired", parse_qs(urlparse(r["Location"]).query)["error"][0])

    # ── administration ──
    def test_admin_imports_metadata_and_cannot_enable_an_incomplete_provider(self):
        c = APIClient()
        c.force_authenticate(self.admin)
        cert_b64 = "".join(CERT2.strip().splitlines()[1:-1])
        metadata = (f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
                    f'xmlns:ds="http://www.w3.org/2000/09/xmldsig#" entityID="https://new-idp.test/">'
                    f'<md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
                    f'<md:KeyDescriptor use="signing"><ds:KeyInfo><ds:X509Data><ds:X509Certificate>{cert_b64}</ds:X509Certificate>'
                    f'</ds:X509Data></ds:KeyInfo></md:KeyDescriptor>'
                    f'<md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" '
                    f'Location="https://new-idp.test/sso"/></md:IDPSSODescriptor></md:EntityDescriptor>')
        r = c.patch("/api/v1/auth/admin/sso/", {"protocol": "saml", "saml_metadata_xml": metadata}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual((body["saml_idp_entity_id"], body["saml_idp_sso_url"]), ("https://new-idp.test/", "https://new-idp.test/sso"))
        self.assertEqual(body["saml_certificates"][0]["subject"], "CN=idp-rollover.test")
        self.assertEqual((body["saml_sp_entity_id"], body["saml_acs_url"]), (SP_ENTITY, ACS))
        self.assertTrue(c.post("/api/v1/auth/admin/sso/test/").json()["ok"])
        # Bad input is refused.
        self.assertEqual(c.patch("/api/v1/auth/admin/sso/", {"saml_idp_certificates": "not a cert"}, format="json").status_code, 400)
        self.assertEqual(c.patch("/api/v1/auth/admin/sso/", {"saml_idp_sso_url": "http://plain.test"}, format="json").status_code, 400)
        self.assertEqual(c.patch("/api/v1/auth/admin/sso/", {"saml_metadata_xml": "<!DOCTYPE x><x/>"}, format="json").status_code, 400)
        self.assertEqual(c.patch("/api/v1/auth/admin/sso/", {"protocol": "ws-fed"}, format="json").status_code, 400)
        r = c.patch("/api/v1/auth/admin/sso/", {"saml_idp_certificates": "", "enabled": True}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("signing certificate", r.json()["error"])

    def test_only_administrators_change_saml_settings(self):
        c = APIClient()
        c.force_authenticate(self.lawyer)
        self.assertEqual(c.patch("/api/v1/auth/admin/sso/", {"protocol": "oidc"}, format="json").status_code, 403)
