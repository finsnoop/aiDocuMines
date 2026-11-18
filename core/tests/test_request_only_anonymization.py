"""Uploads retain their originals; only an explicit action schedules anonymization."""
import contextlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.bulk_registry import get_bulk_handler
from core.ingest import MatterBatch
from core.models import EndpointResponseTable, File, Matter, Run
from core.tests.test_tenancy import make_user, token_for
from core.views import finalize_uploaded_file
from custom_authentication.models import Client
from document_anonymizer.models import AnonymizationRun


class RequestOnlyAnonymizationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Request-only firm")
        cls.other_firm = Client.objects.create(name="Other firm")
        cls.owner = make_user("owner@request-only.test", cls.firm, groups=["Client"])
        cls.colleague = make_user("colleague@request-only.test", cls.firm, groups=["Client"])
        cls.outsider = make_user("outsider@request-only.test", cls.other_firm, groups=["Client"])
        cls.guest = make_user("guest@request-only.test", cls.firm, groups=["Guest"])
        cls.tokens = {user.id: token_for(user)[1] for user in [cls.owner, cls.colleague, cls.outsider, cls.guest]}

    def setUp(self):
        cache.clear()
        self.context = contextlib.ExitStack()
        self.addCleanup(self.context.close)
        self.media = self.context.enter_context(tempfile.TemporaryDirectory())
        self.context.enter_context(override_settings(MEDIA_ROOT=self.media))
        self.anon = self.context.enter_context(mock.patch("document_anonymizer.tasks.anonymize_document_task.apply_async"))
        self.risk = self.context.enter_context(mock.patch("document_anonymizer.tasks.compute_risk_score_task.apply_async"))
        self.extract = self.context.enter_context(mock.patch("core.views.extract_document_text_task.delay"))
        self.classify = self.context.enter_context(mock.patch("core.views.classify_file.delay"))
        for task in ["index_file.apply_async", "generate_insights_for_user.delay", "process_bulk_metadata.delay"]:
            self.context.enter_context(mock.patch("core.views." + task))

    def api(self, user=None):
        user = user or self.owner
        token = self.tokens[user.id]
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer " + token.token, HTTP_X_CLIENT_ID=token.application.client_id)
        return client

    def assert_no_privacy_jobs(self):
        self.anon.assert_not_called()
        self.risk.assert_not_called()
        self.assertFalse(AnonymizationRun.objects.exists())
        self.assertFalse(EndpointResponseTable.objects.filter(endpoint_name__icontains="anonym").exists())

    def matter(self):
        matter = Matter.objects.create(client=self.firm, name="Explicit processing matter", created_by=self.owner)
        matter.members.add(self.owner)
        return matter

    def saved_file(self, user=None):
        user = user or self.owner
        path = Path(self.media) / f"{user.id}-manual.txt"
        path.write_text("Original contract with a named party.", encoding="utf-8")
        run = Run.objects.create(user=user, status="Completed")
        return File.objects.create(user=user, run=run, filename=path.name, filepath=str(path),
                                   file_type="text/plain", project_id="manual", service_id="documents")

    def test_standard_upload_does_not_schedule_anonymization_or_privacy_risk(self):
        for name, mime, content in [
            ("contract.pdf", "application/pdf", b"%PDF-1.4 original contract"),
            ("contract.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"original office contract"),
            ("notes.txt", "text/plain", b"original confidential notes"),
        ]:
            with self.subTest(name=name), self.captureOnCommitCallbacks(execute=True):
                response = self.api().post("/api/v1/core/upload/", {
                    "project_id": "uploads", "service_id": "documents",
                    "file": [SimpleUploadedFile(name, content, content_type=mime)],
                }, format="multipart")
            self.assertEqual(response.status_code, 201, response.content)
            stored = File.objects.get(id=response.json()["files"][0]["file_id"])
            self.assertEqual(Path(stored.filepath).read_bytes(), content)
        self.assert_no_privacy_jobs()
        self.assertEqual(self.extract.call_count, 3)
        self.assertEqual(self.classify.call_count, 3)

    def test_matter_upload_retains_original_without_privacy_processing(self):
        matter = self.matter()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.api().post(f"/api/v1/core/projects/{matter.id}/upload/", {
                "file": [SimpleUploadedFile("matter.pdf", b"%PDF-1.4 matter original", content_type="application/pdf")]
            }, format="multipart")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["uploaded"], 1)
        self.assert_no_privacy_jobs()
        self.extract.assert_called_once()

    def test_connector_ingestion_uses_the_same_request_only_policy(self):
        with self.captureOnCommitCallbacks(execute=True):
            batch = MatterBatch(self.owner, self.matter(), source="nextcloud")
            result = batch.add_stream("connector.txt", [b"Connector original containing personal data."])
        self.assertEqual(result["status"], "uploaded", result)
        self.assert_no_privacy_jobs()
        self.extract.assert_called_once_with(result["file_id"])

    def test_finalized_folder_upload_does_not_schedule_privacy_processing(self):
        source = Path(self.media) / "incoming.txt"
        source.write_bytes(b"Original finalized upload.")
        run = Run.objects.create(user=self.owner, status="Pending")
        with self.captureOnCommitCallbacks(execute=True):
            result = finalize_uploaded_file(user=self.owner, run=run, project_id="folder-upload", service_id="documents",
                                           file_path=str(source), filename="finalized.txt", client_id=str(self.firm.id))
        self.assertEqual(Path(File.objects.get(id=result["file_id"]).filepath).read_bytes(), b"Original finalized upload.")
        self.assert_no_privacy_jobs()
        self.extract.assert_called_once_with(result["file_id"])

    def test_manual_anonymization_still_dispatches_for_an_authorized_owner(self):
        document = self.saved_file()
        with mock.patch("document_anonymizer.views.anonymize_document_task.delay") as dispatch, self.captureOnCommitCallbacks(execute=True):
            response = self.api().post(f"/api/v1/anonymizer/submit-anonymization/?file_id={document.id}&project_id=manual&service_id=documents&file_type=plain")
        self.assertEqual(response.status_code, 202, response.content)
        run = AnonymizationRun.objects.get(id=response.json()["anonymization_run_id"])
        self.assertEqual(run.client_id, self.firm.id)
        dispatch.assert_called_once_with(document.id, "plain", str(run.id), user_id=self.owner.pk)

    def test_manual_anonymization_keeps_file_access_and_tenant_checks(self):
        document = self.saved_file()
        with mock.patch("document_anonymizer.views.anonymize_document_task.delay") as dispatch:
            for user in [self.colleague, self.outsider]:
                response = self.api(user).post(f"/api/v1/anonymizer/submit-anonymization/?file_id={document.id}&project_id=manual&service_id=documents")
                self.assertIn(response.status_code, [403, 404], response.content)
        dispatch.assert_not_called()
        self.assertFalse(AnonymizationRun.objects.exists())

    def test_guest_cannot_dispatch_anonymization_even_for_their_own_file(self):
        document = self.saved_file(self.guest)
        with mock.patch("document_anonymizer.views.anonymize_document_task.delay") as dispatch, self.captureOnCommitCallbacks(execute=True):
            response = self.api(self.guest).post(f"/api/v1/anonymizer/submit-anonymization/?file_id={document.id}&project_id=manual&service_id=documents")
        self.assertEqual(response.status_code, 403, response.content)
        dispatch.assert_not_called()

    def test_explicit_bulk_or_workflow_anonymization_handler_still_dispatches(self):
        document = self.saved_file()
        handler = get_bulk_handler("ai_anonymization")
        self.assertIsNotNone(handler)
        with mock.patch("document_anonymizer.tasks.anonymize_document_task.delay") as dispatch:
            handler.call(document.id, self.owner.id)
        run = AnonymizationRun.objects.get()
        dispatch.assert_called_once_with(file_id=document.id, run_id=run.id, file_type="plain", user_id=self.owner.pk)
        self.assertEqual(run.client_id, self.firm.id)

    def test_explicit_privacy_risk_request_still_dispatches(self):
        document = self.saved_file()
        with mock.patch("document_anonymizer.views.compute_risk_score_task.delay", return_value=SimpleNamespace(id="explicit-risk-task")) as dispatch:
            response = self.api().get(f"/api/v1/anonymizer/risk-score/?file_id={document.id}")
        self.assertEqual(response.status_code, 202, response.content)
        dispatch.assert_called_once_with(str(document.id), user_id=self.owner.pk)
