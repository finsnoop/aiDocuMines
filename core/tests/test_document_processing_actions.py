"""Requested processing: HTTP -> real worker -> private persisted artefacts.

These tests use local Tesseract/OCRmyPDF and Presidio/spaCy; no provider calls.
Only queue delivery is intercepted so it runs inside the isolated test database.
"""
import contextlib
import hashlib
import json
from pathlib import Path
import tempfile
from unittest import mock

import fitz
from PIL import Image, ImageDraw, ImageFont
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import File, Metadata, Run, Storage
from core.tests.test_tenancy import make_user, token_for
from custom_authentication.models import Client
from document_operations.models import EffectiveAccess
from document_ocr.models import OCRFile, OCRRun
from document_ocr.tasks import process_ocr
from document_anonymizer.models import Anonymize, AnonymizationRun
from document_anonymizer.tasks import anonymize_document_task, compute_risk_score_task


class DocumentProcessingActionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Processor firm")
        cls.other = Client.objects.create(name="Other processor firm")
        cls.owner = make_user("owner@processor.test", cls.firm, groups=["Client"])
        cls.shared = make_user("shared@processor.test", cls.firm, groups=["Client"])
        cls.peer = make_user("peer@processor.test", cls.firm, groups=["Client"])
        cls.foreign = make_user("foreign@processor.test", cls.other, groups=["Admin"])
        cls.guest = make_user("guest@processor.test", cls.firm, groups=["Guest"])
        cls.tokens = {u.pk: token_for(u)[1] for u in [cls.owner, cls.shared, cls.peer, cls.foreign, cls.guest]}

    def setUp(self):
        cache.clear()
        self.context = contextlib.ExitStack()
        self.addCleanup(self.context.close)
        self.media = Path(self.context.enter_context(tempfile.TemporaryDirectory()))
        self.context.enter_context(override_settings(MEDIA_ROOT=str(self.media)))
        self.root = self.media / "uploads" / str(self.firm.pk) / str(self.owner.pk) / "Contracts" / "documents" / "20261118"
        self.root.mkdir(parents=True)

    def api(self, user=None):
        user = user or self.owner
        token = self.tokens[user.pk]
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer " + token.token, HTTP_X_CLIENT_ID=token.application.client_id)
        return client

    def file(self, name="contract.pdf", text="Contract expires on 31 December 2026.", image=False, owner=None):
        owner = owner or self.owner
        path = self.root / name
        if image:
            raster = Image.new("RGB", (1300, 250), "white")
            font = ImageFont.load_default(size=42)
            ImageDraw.Draw(raster).text((40, 55), text, font=font, fill="black")
            raster.save(path)
        elif name.endswith(".pdf"):
            with fitz.open() as doc:
                doc.new_page().insert_text((50, 70), text, fontsize=14)
                doc.save(path)
        else:
            path.write_text(text, encoding="utf-8")
        run = Run.objects.create(user=owner, status="Completed")
        return File.objects.create(user=owner, run=run, filename=name, filepath=str(path),
            file_size=path.stat().st_size, extension=path.suffix[1:], md5_hash=hashlib.md5(path.read_bytes()).hexdigest(),
            project_id="Contracts", service_id="documents")

    def submit_ocr(self, file, user=None, option="Basic-ocr"):
        with mock.patch("document_ocr.tasks.process_ocr.delay") as dispatch, self.captureOnCommitCallbacks(execute=True):
            response = self.api(user).post(f"/api/v1/ocr/submit-ocr/?file_id={file.pk}&ocr_option={option}")
        return response, dispatch

    def submit_anon(self, file, user=None, kind="structured"):
        with mock.patch("document_anonymizer.tasks.anonymize_document_task.delay") as dispatch, self.captureOnCommitCallbacks(execute=True):
            response = self.api(user).post(f"/api/v1/anonymizer/submit-anonymization/?file_id={file.pk}&file_type={kind}")
        return response, dispatch

    def assert_outputs(self, result, source, expected, actor=None):
        self.assertEqual(result["status"], "Completed", result)
        ids = [item["file_id"] for item in result["registered_outputs"]]
        self.assertEqual(len(set(ids)), expected)
        for file in File.objects.filter(pk__in=ids):
            self.assertTrue(Path(file.filepath).is_file())
            self.assertGreater(Path(file.filepath).stat().st_size, 0)
            self.assertEqual((file.user_id, file.project_id, file.service_id, file.origin_file_id),
                             (source.user_id, source.project_id, source.service_id, source.pk))
            self.assertEqual(file.storage.output_storage_location, file.filepath)
            self.assertEqual(file.folder_links.count(), 1)
            response = self.api(actor).get(f"/api/v1/core/download/?file_id={file.pk}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), Path(file.filepath).read_bytes())
            for denied in [self.peer, self.foreign]:
                self.assertEqual(self.api(denied).get(f"/api/v1/core/download/?file_id={file.pk}").status_code, 404)
        return ids

    def test_basic_pdf_ocr_from_http_to_completed_outputs_and_scoped_status(self):
        file = self.file()
        response, dispatch = self.submit_ocr(file)
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(dispatch.call_count, 1)
        result = process_ocr(*dispatch.call_args.args, **dispatch.call_args.kwargs)
        self.assert_outputs(result, file, 3)
        run = OCRRun.objects.get(pk=response.json()["ocr_run_id"])
        self.assertEqual(run.status, "Completed")
        file.refresh_from_db()
        self.assertIn("Contract expires", file.content)
        status = self.api().get(f"/api/v1/ocr/ocr-task-status/?run_id={run.pk}")
        self.assertEqual(status.status_code, 200, status.content)
        self.assertEqual(len(status.json()["registered_outputs"]), 3)
        self.assertEqual(self.api(self.peer).get(f"/api/v1/ocr/ocr-task-status/?run_id={run.pk}").status_code, 404)
        download = self.api().get(f"/api/v1/ocr/download-file/?file_id={file.pk}&file_type=ocr_pdf")
        self.assertEqual(download.status_code, 200)
        b"".join(download.streaming_content)

    def test_explicit_image_ocr_really_recognises_text_and_grants_requester_output(self):
        file = self.file("scan.png", text="Agreement invoice total 2500", image=True)
        EffectiveAccess.objects.create(user=self.shared, file=file, can_download=True)
        response, dispatch = self.submit_ocr(file, self.shared, "Advanced-ocr")
        self.assertEqual(response.status_code, 202, response.content)
        result = process_ocr(*dispatch.call_args.args, **dispatch.call_args.kwargs)
        self.assert_outputs(result, file, 3, self.shared)
        file.refresh_from_db()
        self.assertIn("Agreement", file.content)
        self.assertIn("2500", file.content)

    def test_duplicate_queued_click_uses_same_job_and_native_preview_does_not_ocr(self):
        file = self.file()
        first, dispatch = self.submit_ocr(file)
        second, other = self.submit_ocr(file)
        self.assertEqual(first.json()["ocr_run_id"], second.json()["ocr_run_id"])
        other.assert_not_called()
        self.assertEqual(OCRRun.objects.count(), 1)
        with mock.patch("document_search.utils._ocr_pdf_for_text") as ocr:
            preview = self.api().get(f"/api/v1/grid-documents-interrogation/queries/ocr-preview/?file_id={file.pk}")
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertTrue(preview.json()["ready"])
        ocr.assert_not_called()

    def test_real_anonymization_masks_pii_in_every_export_and_keeps_mapping_private(self):
        file = self.file("private.txt", text="Contact alice@example.com for this agreement. <script>alert(1)</script>")
        response, dispatch = self.submit_anon(file)
        self.assertEqual(response.status_code, 202, response.content)
        result = anonymize_document_task(*dispatch.call_args.args, **dispatch.call_args.kwargs)
        self.assert_outputs(result, file, 3)
        record = Anonymize.objects.get(run_id=response.json()["anonymization_run_id"])
        self.assertEqual(record.status, "Completed")
        self.assertIn("alice@example.com", Path(record.entity_mapping_filepath).read_text())
        self.assertFalse(File.objects.filter(filepath=record.entity_mapping_filepath).exists())
        for output in File.objects.filter(pk__in=[item["file_id"] for item in result["registered_outputs"]]):
            text = Path(output.filepath).read_text()
            self.assertNotIn("alice@example.com", text)
            if output.filename.endswith(".html"):
                self.assertNotIn("<script>", text)
                self.assertIn("&lt;script&gt;", text)
        status = self.api().get(f"/api/v1/anonymizer/check-anonymization-status/?run_id={record.run_id}")
        self.assertEqual(status.status_code, 200, status.content)
        self.assertEqual(len(status.json()["registered_outputs"]), 3)
        for representation in ["plain", "structured", "html", "json"]:
            download = self.api().get(f"/api/v1/anonymizer/download-anonymized-file/?file_id={file.pk}&file_type={representation}&output_type=structured")
            self.assertEqual(download.status_code, 200, download.content if not download.streaming else "stream")
            if download.streaming:
                b"".join(download.streaming_content)
            else:
                self.assertNotIn("alice@example.com", download.content.decode())

        for output_type, endpoint in [("docx", "download-structured-docx"), ("markdown", "download-structured-markdown")]:
            exported = self.api().post("/api/v1/anonymizer/export-blocks/", {"file_id": file.pk, "export_format": output_type}, format="json")
            self.assertEqual(exported.status_code, 200, exported.content)
            generated = File.objects.get(pk=exported.json()["file_id"])
            self.assertTrue(generated.folder_links.exists())
            self.assertTrue(Path(generated.filepath).is_file())
            download = self.api().get(f"/api/v1/anonymizer/{endpoint}/?file_id={file.pk}")
            self.assertEqual(download.status_code, 200)
            b"".join(download.streaming_content)

    def test_rejected_roles_and_tenants_never_queue_processors(self):
        file = self.file()
        for actor in [self.peer, self.foreign]:
            for submit in [self.submit_ocr, self.submit_anon]:
                response, task = submit(file, actor)
                self.assertIn(response.status_code, [403, 404], response.content)
                task.assert_not_called()
        guest_file = self.file("guest.pdf", owner=self.guest)
        for submit in [self.submit_ocr, self.submit_anon]:
            response, task = submit(guest_file, self.guest)
            self.assertEqual(response.status_code, 403)
            task.assert_not_called()

    def test_worker_rechecks_revoked_shared_access_before_processing(self):
        file = self.file()
        grant = EffectiveAccess.objects.create(user=self.shared, file=file, can_download=True)
        response, dispatch = self.submit_ocr(file, self.shared)
        self.assertEqual(response.status_code, 202)
        grant.delete()
        with mock.patch("document_ocr.utils.OCRService.apply_ocr") as engine:
            result = process_ocr(*dispatch.call_args.args, **dispatch.call_args.kwargs)
        self.assertEqual(result["status"], "Failed")
        engine.assert_not_called()
        self.assertFalse(File.objects.filter(origin_file=file).exists())

    def test_explicit_risk_assessment_is_persisted_without_anonymized_outputs(self):
        file = self.file("risk.txt", text="Email alice@example.com about this account.")
        result = compute_risk_score_task(file.pk, user_id=self.owner.pk)
        self.assertEqual(result["status"], "Completed", result)
        saved = json.loads(Metadata.objects.get(file=file).custom_metadata)["privacy_risk"]
        self.assertEqual(saved["risk_score"], result["risk_score"])
        self.assertEqual(saved["requested_by"], self.owner.pk)
        self.assertFalse(Anonymize.objects.exists())
        self.assertFalse(File.objects.filter(origin_file=file).exists())


class OutputPublicationGuardTests(DocumentProcessingActionTests):
    # Reuse fixture helpers without running the real processor cases twice.
    test_basic_pdf_ocr_from_http_to_completed_outputs_and_scoped_status = None
    test_explicit_image_ocr_really_recognises_text_and_grants_requester_output = None
    test_duplicate_queued_click_uses_same_job_and_native_preview_does_not_ocr = None
    test_real_anonymization_masks_pii_in_every_export_and_keeps_mapping_private = None
    test_rejected_roles_and_tenants_never_queue_processors = None
    test_worker_rechecks_revoked_shared_access_before_processing = None
    test_explicit_risk_assessment_is_persisted_without_anonymized_outputs = None

    def test_changed_source_or_actor_during_output_hash_cannot_publish(self):
        from django.contrib.auth import get_user_model
        from core.processing import source_version
        from core.utils import register_generated_file
        from rest_framework.exceptions import PermissionDenied
        real_md5 = hashlib.md5
        file = self.file()
        output = self.root / "derived.txt"
        output.write_text("Derived confidential output", encoding="utf-8")
        cases = {
            "disabled": lambda: get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False),
            "tenant_transfer": lambda: get_user_model().objects.filter(pk=self.owner.pk).update(client=self.other),
            "matter_move": lambda: File.objects.filter(pk=file.pk).update(project_id="Different matter"),
            "role_revoked": lambda: self.owner.groups.set(self.guest.groups.all()),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                get_user_model().objects.filter(pk=self.owner.pk).update(is_active=True, client=self.firm)
                self.owner.groups.set(self.peer.groups.all())
                File.objects.filter(pk=file.pk).update(project_id="Contracts")
                self.owner.refresh_from_db()
                file.refresh_from_db()
                version = source_version(file)
                storage_count = Storage.objects.count()
                digest = real_md5()
                class ChangingHash:
                    def update(self, chunk):
                        digest.update(chunk)
                        mutate()
                    def hexdigest(self):
                        return digest.hexdigest()
                with mock.patch("core.utils.hashlib.md5", return_value=ChangingHash()), self.assertRaises(
                        (get_user_model().DoesNotExist, PermissionDenied, PermissionError, ValueError)):
                    register_generated_file(str(output), self.owner, file.run, file.project_id, file.service_id,
                        source_file=file, actor=self.owner, expected_source_version=version, permission="ocr.submit")
                self.assertFalse(File.objects.filter(filepath=str(output)).exists())
                self.assertEqual(Storage.objects.count(), storage_count)
