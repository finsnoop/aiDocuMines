"""E-signature: sending, completion (signed copy stored next to the contract, contract made active),
webhook verification (never trusted for status), voiding, permissions and organisation scoping."""
import base64
import hashlib
import hmac
import json
import os
import tempfile
from datetime import date
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import File, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from document_operations.models import FileFolderLink
from document_operations.utils import get_or_create_folder_tree
from integrations.models import ConnectorAccount
from integrations.providers.esign import DocuSign
from .esign import refresh
from .models import Contract, SignatureRequest

HMAC_KEY = "connect-hmac-secret-for-tests"
SIGNERS = [{"name": "Ann Buyer", "email": "ann@acme.test"}, {"name": "Sam Seller", "email": "sam@firm.test", "order": 2}]


class ESignTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Sign firm")
        cls.other = Client.objects.create(name="Other sign firm")
        cls.legal = make_user("legal@sign.test", cls.firm, groups=["Manager"])
        cls.member = make_user("member@sign.test", cls.firm, groups=["Client"])
        cls.outsider = make_user("outsider@othersign.test", cls.other, groups=["Admin"])

    def setUp(self):
        cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        media = override_settings(MEDIA_ROOT=self.tmp.name)
        media.enable()
        self.addCleanup(media.disable)
        folder_path = os.path.join(self.tmp.name, "uploads", "deal")
        os.makedirs(folder_path)
        path = os.path.join(folder_path, "msa.pdf")
        with open(path, "wb") as fh:
            fh.write(b"%PDF-1.4 unsigned")
        self.doc = File.objects.create(user=self.legal, run=Run.objects.create(user=self.legal), filename="msa.pdf",
                                       filepath=path, project_id="Deals", service_id="matter", md5_hash="unsigned")
        FileFolderLink.objects.create(file=self.doc, folder=get_or_create_folder_tree(["Deal"], user=self.legal,
                                                                                     project_id="Deals", service_id="matter"))
        self.contract = Contract.objects.create(client=self.firm, title="Acme MSA", stage="signature", owner=self.legal,
                                                document=self.doc, start_date=date.today())
        ConnectorAccount.objects.create(user=self.legal, service="docusign", access_token="token",
                                        extra={"account_id": "acc-1", "base_uri": "https://demo.docusign.test"})

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def send(self, user=None, **body):
        body = {"provider": "docusign", "signers": SIGNERS, "message": "Please sign", **body}
        with mock.patch.object(DocuSign, "send", return_value="env-123") as send:
            r = self.api(user or self.legal).post(f"/api/v1/contracts/contracts/{self.contract.id}/signature/", body, format="json")
        return r, send

    def test_send_requires_the_signature_stage_a_connection_and_valid_signers(self):
        Contract.objects.filter(pk=self.contract.pk).update(stage="review")
        self.assertEqual(self.send()[0].status_code, 400)
        Contract.objects.filter(pk=self.contract.pk).update(stage="signature")
        self.assertEqual(self.send(signers=[{"name": "x", "email": "not-an-email"}])[0].status_code, 400)
        self.assertEqual(self.send(signers=SIGNERS + [SIGNERS[0]])[0].status_code, 400)       # duplicate signer
        self.assertEqual(self.send(provider="adobe_sign")[0].status_code, 400)                 # not connected
        r, send = self.send()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["status"], "sent")
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs["pdf_bytes"], b"%PDF-1.4 unsigned")
        self.assertEqual([s["email"] for s in kwargs["signers"]], ["ann@acme.test", "sam@firm.test"])
        self.assertEqual(self.send()[0].status_code, 400)                                      # already out for signature

    def test_completion_stores_the_signed_copy_and_activates_the_contract(self):
        self.send()
        req = SignatureRequest.objects.get()
        with mock.patch.object(DocuSign, "status", return_value={"status": "completed", "signers": [
                    {"email": "ann@acme.test", "status": "completed", "signed_at": "2026-11-21T10:00:00Z"}]}), \
                mock.patch.object(DocuSign, "signed_pdf", return_value=b"%PDF-1.4 signed"), \
                mock.patch.object(DocuSign, "certificate", return_value=b"%PDF-1.4 certificate"):
            self.assertEqual(refresh(req), "completed")
        req.refresh_from_db()
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.stage, "active")
        self.assertEqual(self.contract.document, req.signed_file)
        signed = req.signed_file
        self.assertEqual((signed.user, signed.project_id, signed.origin_file), (self.legal, "Deals", self.doc))
        with open(signed.filepath, "rb") as fh:
            self.assertEqual(fh.read(), b"%PDF-1.4 signed")
        self.assertTrue(signed.filepath.startswith(self.tmp.name))
        self.assertTrue(FileFolderLink.objects.filter(file=signed).exists())
        self.assertIsNotNone(req.certificate_file)
        self.assertEqual(req.signers[0]["status"], "completed")

    def test_declined_is_recorded_and_the_contract_stays_put(self):
        self.send()
        req = SignatureRequest.objects.get()
        with mock.patch.object(DocuSign, "status", return_value={"status": "declined", "signers": []}):
            self.assertEqual(refresh(req), "declined")
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.stage, "signature")
        self.assertTrue(self.contract.events.filter(kind="signature", note__icontains="declined").exists())

    @mock.patch.dict(os.environ, {"DOCUSIGN_CONNECT_HMAC_KEY": HMAC_KEY})
    def test_docusign_webhook_must_be_signed_and_only_triggers_a_re_check(self):
        self.send()
        body = json.dumps({"event": "envelope-completed", "data": {"envelopeId": "env-123", "status": "completed"}}).encode()
        good = base64.b64encode(hmac.new(HMAC_KEY.encode(), body, hashlib.sha256).digest()).decode()
        url = "/api/v1/contracts/signature/webhook/docusign/"
        with mock.patch("contracts.esign.esign_refresh_task.delay") as queue:
            r = APIClient().post(url, body, content_type="application/json", HTTP_X_DOCUSIGN_SIGNATURE_1="forged")
            self.assertEqual(r.status_code, 401)
            queue.assert_not_called()
            r = APIClient().post(url, body, content_type="application/json", HTTP_X_DOCUSIGN_SIGNATURE_1=good)
            self.assertEqual(r.status_code, 200)
            queue.assert_called_once()
        # The payload's "completed" changed nothing by itself.
        self.assertEqual(SignatureRequest.objects.get().status, "sent")
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.stage, "signature")

    @mock.patch.dict(os.environ, {"ADOBE_SIGN_CLIENT_ID": "adobe-app"})
    def test_adobe_webhook_verifies_and_echoes_the_client_id(self):
        url = "/api/v1/contracts/signature/webhook/adobe_sign/"
        self.assertEqual(APIClient().get(url, HTTP_X_ADOBESIGN_CLIENTID="someone-else").status_code, 401)
        r = APIClient().get(url, HTTP_X_ADOBESIGN_CLIENTID="adobe-app")
        self.assertEqual(r["X-AdobeSign-ClientId"], "adobe-app")

    def test_void_and_permissions(self):
        self.send()
        req = SignatureRequest.objects.get()
        self.assertEqual(self.send(user=self.member)[0].status_code, 404)              # cannot see the contract
        self.assertEqual(self.api(self.outsider).post(f"/api/v1/contracts/signature/{req.id}/void/", {"reason": "x"},
                                                      format="json").status_code, 404)
        self.assertEqual(self.api(self.legal).post(f"/api/v1/contracts/signature/{req.id}/void/", {}, format="json").status_code, 400)
        with mock.patch.object(DocuSign, "void") as void:
            r = self.api(self.legal).post(f"/api/v1/contracts/signature/{req.id}/void/", {"reason": "Wrong version"}, format="json")
        self.assertEqual(r.json()["status"], "voided")
        void.assert_called_once()

    def test_providers_are_registered_for_integrations(self):
        from integrations.providers import get_provider
        self.assertEqual(get_provider("docusign").category, "E-signature")
        self.assertFalse(get_provider("adobe_sign").can_import)
