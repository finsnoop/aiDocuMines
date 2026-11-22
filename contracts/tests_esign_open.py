"""Open-source e-signature services (DocuSeal, Documenso, SignuDoc): organisation connections set by administrators,
the appended signatures page, sending and completion through the same flow as DocuSign, webhook verification, and
safety checks on addresses. The services are mocked at the single network entry point."""
import base64
import hashlib
import hmac
import io
import json
import os
import tempfile
import time
from datetime import date
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import File, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from document_operations.models import FileFolderLink
from document_operations.utils import get_or_create_folder_tree
from integrations.models import ESignConnection
from integrations.providers import ConnectorError
from integrations.providers.esign_open import OrgApi, SignuDoc, check_base_url, signature_page, standard_webhook_ok
from .esign import refresh
from .models import Contract, SignatureRequest

NET = "integrations.providers.esign_open.http_request"
SIGNERS = [{"name": "Ann Buyer", "email": "ann@acme.test"}, {"name": "Sam Seller", "email": "sam@firm.test", "order": 2}]


def pdf_bytes(pages=1):
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i in range(pages):
        c.drawString(72, 720, f"Master services agreement, page {i + 1}")
        c.showPage()
    c.save()
    return buf.getvalue()


def resp(status=200, body=None, content=b"", ctype="application/json"):
    return SimpleNamespace(status_code=status, json=lambda: body if body is not None else {}, content=content,
                           headers={"content-type": ctype})


class SignaturePageTests(TestCase):
    def test_appends_one_box_per_signer_without_touching_the_contract(self):
        from pypdf import PdfReader
        signers = [{"name": f"Signer {i}", "email": f"s{i}@x.test"} for i in range(10)]
        out, boxes = signature_page(pdf_bytes(3), signers)
        self.assertEqual(len(PdfReader(io.BytesIO(out)).pages), 3 + 2)        # 8 boxes per page
        self.assertEqual([b["page"] for b in boxes[:9]], [4] * 8 + [5])
        self.assertTrue(all(0 < b["x"] < 1 and 0 < b["y"] < 1 and b["x"] + b["w"] < 1 and b["y"] + b["h"] < 1 for b in boxes))
        self.assertIn("Master services agreement, page 1", PdfReader(io.BytesIO(out)).pages[0].extract_text())

    def test_unreadable_pdf_is_refused(self):
        with self.assertRaises(ConnectorError):
            signature_page(b"not a pdf", SIGNERS)

    def test_addresses_must_be_https_except_our_internal_services(self):
        self.assertEqual(check_base_url("https://sign.firm.test/api/"), "https://sign.firm.test/api")
        self.assertEqual(check_base_url("http://docuseal:3000/api"), "http://docuseal:3000/api")
        for bad in ("http://sign.firm.test/api", "ftp://x", "https://user:pw@sign.test", ""):
            with self.assertRaises(ConnectorError):
                check_base_url(bad)

    def test_files_are_only_downloaded_from_the_service_host(self):
        from integrations.providers.esign_open import DocuSeal
        api = OrgApi(DocuSeal(), SimpleNamespace(base_url="https://sign.firm.test/api", api_token="t"))
        with self.assertRaises(ConnectorError):
            api.fetch_file("https://evil.test/file.pdf")
        with mock.patch(NET, return_value=resp(content=b"%PDF signed", ctype="application/pdf")) as net:
            self.assertEqual(api.fetch_file("https://sign.firm.test/file/abc.pdf"), b"%PDF signed")
        self.assertEqual(net.call_args.kwargs["allow_redirects"], False)

    def test_standard_webhook_signature(self):
        secret_key = b"0123456789abcdef0123456789abcdef"
        secret = "whsec_" + base64.b64encode(secret_key).decode()
        body, ts, mid = b'{"type":"envelope.completed"}', str(int(time.time())), "msg_1"
        sig = base64.b64encode(hmac.new(secret_key, f"{mid}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
        self.assertTrue(standard_webhook_ok(secret, mid, ts, f"v1,{sig}", body))
        self.assertTrue(standard_webhook_ok(secret, mid, ts, f"v1,old v1,{sig}", body))       # rotation
        self.assertFalse(standard_webhook_ok(secret, mid, ts, f"v1,{sig}", body + b" "))
        self.assertFalse(standard_webhook_ok(secret, mid, str(int(time.time()) - 900), f"v1,{sig}", body))


class OpenESignFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Open sign firm")
        cls.other = Client.objects.create(name="Other open sign firm")
        cls.admin = make_user("admin@opensign.test", cls.firm, groups=["Admin"])
        cls.legal = make_user("legal@opensign.test", cls.firm, groups=["Manager"])
        cls.member = make_user("member@opensign.test", cls.firm, groups=["Client"])
        cls.outsider = make_user("otheradmin@otheropensign.test", cls.other, groups=["Admin"])

    def setUp(self):
        cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        media = override_settings(MEDIA_ROOT=self.tmp.name)
        media.enable()
        self.addCleanup(media.disable)
        folder = os.path.join(self.tmp.name, "uploads", "deal")
        os.makedirs(folder)
        path = os.path.join(folder, "msa.pdf")
        with open(path, "wb") as fh:
            fh.write(pdf_bytes())
        self.doc = File.objects.create(user=self.legal, run=Run.objects.create(user=self.legal), filename="msa.pdf",
                                       filepath=path, project_id="Deals", service_id="matter", md5_hash="msa")
        FileFolderLink.objects.create(file=self.doc, folder=get_or_create_folder_tree(["Deal"], user=self.legal,
                                                                                     project_id="Deals", service_id="matter"))
        self.contract = Contract.objects.create(client=self.firm, title="Acme MSA", stage="signature", owner=self.legal,
                                                document=self.doc, start_date=date.today())

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def connect(self, provider, **extra):
        body = {"provider": provider, "base_url": f"https://{provider}.firm.test/api", "api_token": "tok-123", **extra}
        with mock.patch.dict("os.environ", {"ESIGN_PUBLIC_API_URL": "https://api.firm.test"}):
            return self.api(self.admin).put("/api/v1/contracts/signature/connections/", body, format="json")

    def test_only_administrators_connect_and_the_token_is_never_returned(self):
        self.assertEqual(self.api(self.member).put("/api/v1/contracts/signature/connections/",
                                                   {"provider": "documenso"}, format="json").status_code, 403)
        r = self.connect("documenso")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertTrue(body["token_configured"])
        self.assertNotIn("tok-123", json.dumps(body))
        token = ESignConnection.objects.get().webhook_token
        self.assertEqual(body["webhook_url"], f"https://api.firm.test/api/v1/contracts/signature/webhook/documenso/{token}/")
        self.assertEqual(self.connect("documenso", base_url="http://documenso.firm.test").status_code, 400)
        listing = self.api(self.admin).get("/api/v1/contracts/signature/connections/").json()["results"]
        self.assertEqual([c["provider"] for c in listing], ["docuseal", "documenso", "signudoc"])
        # Another organisation sees none of it.
        other = self.api(self.outsider).get("/api/v1/contracts/signature/connections/").json()["results"]
        self.assertFalse(any(c["connected"] for c in other))

    def test_people_are_offered_the_services_that_are_connected(self):
        self.connect("signudoc")
        self.connect("docuseal", enabled=False)
        rows = {r["key"]: r for r in self.api(self.legal).get("/api/v1/contracts/signature/providers/").json()["results"]}
        self.assertTrue(rows["signudoc"]["available"])
        self.assertFalse(rows["docuseal"]["available"])                 # switched off by the administrator
        self.assertFalse(rows["docusign"]["available"])                 # this person has not connected DocuSign

    def test_documenso_send_and_completion(self):
        self.connect("documenso")
        calls = []

        def net(method, url, **kw):
            calls.append((method, url, kw))
            if url.endswith("/envelope/create"):
                payload = json.loads(kw["data"]["payload"])
                self.assertEqual(payload["meta"]["signingOrder"], "SEQUENTIAL")
                self.assertEqual([r["fields"][0]["type"] for r in payload["recipients"]], ["SIGNATURE", "SIGNATURE"])
                self.assertEqual(kw["headers"]["Authorization"], "tok-123")
                return resp(body={"id": "envelope_abc"})
            if url.endswith("/envelope/distribute"):
                return resp(body={"success": True})
            if url.endswith("/envelope/envelope_abc"):
                return resp(body={"status": "COMPLETED", "envelopeItems": [{"id": "item_1"}],
                                  "recipients": [{"email": "ann@acme.test", "signingStatus": "SIGNED", "signedAt": "2026-11-22T10:00:00Z"},
                                                 {"email": "sam@firm.test", "signingStatus": "SIGNED", "signedAt": "2026-11-22T11:00:00Z"}]})
            if "/envelope/item/item_1/download" in url:
                self.assertEqual(kw["params"], {"version": "signed"})
                return resp(content=b"%PDF signed by both", ctype="application/pdf")
            raise AssertionError(url)

        with mock.patch(NET, side_effect=net):
            r = self.api(self.legal).post(f"/api/v1/contracts/contracts/{self.contract.id}/signature/",
                                          {"provider": "documenso", "signers": SIGNERS}, format="json")
            self.assertEqual(r.status_code, 201, r.content)
            req = SignatureRequest.objects.get()
            self.assertEqual((req.provider, req.envelope_id, req.status), ("documenso", "envelope_abc", "sent"))
            self.assertEqual(refresh(req), "completed")
        req.refresh_from_db()
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.stage, "active")
        with open(req.signed_file.filepath, "rb") as fh:
            self.assertEqual(fh.read(), b"%PDF signed by both")
        self.assertIsNone(req.certificate_file)                 # Documenso appends its certificate to the PDF

    def test_docuseal_without_pdf_api_gives_a_clear_reason(self):
        self.connect("docuseal")
        with mock.patch(NET, return_value=resp(404, {"error": "Not found"})):
            r = self.api(self.legal).post(f"/api/v1/contracts/contracts/{self.contract.id}/signature/",
                                          {"provider": "docuseal", "signers": SIGNERS}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("DocuSeal Pro", r.json()["error"])

    def test_docuseal_send_uses_text_tags_and_preserved_order(self):
        self.connect("docuseal")
        seen = {}

        def net(method, url, **kw):
            seen.update(kw["json"])
            self.assertEqual(kw["headers"]["X-Auth-Token"], "tok-123")
            return resp(body={"id": 77, "submitters": []})
        with mock.patch(NET, side_effect=net):
            r = self.api(self.legal).post(f"/api/v1/contracts/contracts/{self.contract.id}/signature/",
                                          {"provider": "docuseal", "signers": SIGNERS}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(seen["order"], "preserved")
        self.assertEqual([s["role"] for s in seen["submitters"]], ["Signer 1", "Signer 2"])
        from pypdf import PdfReader
        text = PdfReader(io.BytesIO(base64.b64decode(seen["documents"][0]["file"]))).pages[-1].extract_text()
        self.assertIn("{{Signature;role=Signer 1;type=signature}}", text)

    def test_not_connected_service_is_refused_with_guidance(self):
        r = self.api(self.legal).post(f"/api/v1/contracts/contracts/{self.contract.id}/signature/",
                                      {"provider": "signudoc", "signers": SIGNERS}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("not connected for your organisation", r.json()["error"])

    def test_webhooks_are_verified_and_scoped_to_the_organisation(self):
        secret_key = b"k" * 32
        self.connect("signudoc", webhook_secret="whsec_" + base64.b64encode(secret_key).decode())
        conn = ESignConnection.objects.get()
        req = SignatureRequest.objects.create(client=self.firm, contract=self.contract, provider="signudoc",
                                              envelope_id="env_9", status="sent", source_file=self.doc, sent_by=self.legal)
        body = json.dumps({"type": "envelope.completed", "data": {"envelope": {"id": "env_9"}}}).encode()
        ts, mid = str(int(time.time())), "msg_9"
        sig = base64.b64encode(hmac.new(secret_key, f"{mid}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
        url = f"/api/v1/contracts/signature/webhook/signudoc/{conn.webhook_token}/"
        client = APIClient()
        with mock.patch("contracts.esign.esign_refresh_task.delay") as queued:
            bad = client.post(url, body, content_type="application/json", HTTP_WEBHOOK_ID=mid, HTTP_WEBHOOK_TIMESTAMP=ts,
                              HTTP_WEBHOOK_SIGNATURE="v1,forged")
            self.assertEqual(bad.status_code, 401)
            self.assertEqual(client.post("/api/v1/contracts/signature/webhook/signudoc/wrong-token/", body,
                                         content_type="application/json").status_code, 404)
            ok = client.post(url, body, content_type="application/json", HTTP_WEBHOOK_ID=mid, HTTP_WEBHOOK_TIMESTAMP=ts,
                             HTTP_WEBHOOK_SIGNATURE=f"v1,{sig}")
            self.assertEqual(ok.status_code, 200)
        queued.assert_called_once_with(str(req.pk))

    def test_documenso_webhook_secret_header(self):
        self.connect("documenso", webhook_secret="shared-secret")
        conn = ESignConnection.objects.get()
        url = f"/api/v1/contracts/signature/webhook/documenso/{conn.webhook_token}/"
        body = json.dumps({"event": "DOCUMENT_COMPLETED", "payload": {"envelopeId": "envelope_x"}})
        client = APIClient()
        with mock.patch("contracts.esign.esign_refresh_task.delay"):
            self.assertEqual(client.post(url, body, content_type="application/json", HTTP_X_DOCUMENSO_SECRET="nope").status_code, 401)
            self.assertEqual(client.post(url, body, content_type="application/json",
                                         HTTP_X_DOCUMENSO_SECRET="shared-secret").status_code, 200)

    def test_signudoc_size_limit(self):
        self.connect("signudoc")
        with open(self.doc.filepath, "wb") as fh:
            fh.write(b"%PDF" + b"0" * (5 * 1024 * 1024))
        r = self.api(self.legal).post(f"/api/v1/contracts/contracts/{self.contract.id}/signature/",
                                      {"provider": "signudoc", "signers": SIGNERS}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("SignuDoc", r.json()["error"])
        self.assertEqual(SignuDoc.max_pdf_bytes, int(4.5 * 1024 * 1024))
