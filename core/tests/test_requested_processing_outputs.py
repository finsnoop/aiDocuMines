"""Requested translation/structure/metadata persist real output and enforce source ACLs."""
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import fitz
from docx import Document
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import File, Metadata, Run, Storage
from core.tests.test_assistant import make_doc
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from document_operations.models import EffectiveAccess, FileFolderLink
from document_translation.models import TranslationFile, TranslationRun, TranslationStorage


class ProcessingOutputTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Client.objects.create(name="Processing firm")
        cls.other_org = Client.objects.create(name="Other processing firm")
        cls.owner = make_user("processor-owner@example.test", cls.org, groups=["Client"])
        cls.requester = make_user("processor-requester@example.test", cls.org, groups=["Client"])
        cls.foreign = make_user("processor-foreign@example.test", cls.other_org, groups=["Client"])
        cls.unshared = make_user("processor-unshared@example.test", cls.org, groups=["Client"])

    def setUp(self):
        cache.clear()
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "source.docx"
        document = Document()
        document.add_paragraph("Original contract notice period is thirty days.")
        document.core_properties.title = "Contract title"
        document.core_properties.author = "Test author"
        document.core_properties.last_modified_by = "Test editor"
        document.core_properties.created = datetime(2026, 1, 2, tzinfo=timezone.utc)
        document.save(self.path)
        self.source = make_doc(self.owner, "source.docx", "")
        File.objects.filter(pk=self.source.pk).update(filepath=str(self.path))
        self.source.refresh_from_db()
        EffectiveAccess.objects.create(user=self.requester, file=self.source, can_download=True)
        patcher = mock.patch("oauth2_provider.contrib.rest_framework.TokenHasReadWriteScope.has_permission", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def grant_structure_analysis(self):
        """Structure analysis needs structure.analyze, which the Developer role holds (Client does not)."""
        from django.contrib.auth.models import Group
        self.requester.groups.add(Group.objects.get_or_create(name="Developer")[0])
        self.requester.__dict__.pop("_aidoc_perms", None)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def translation_run(self, user=None):
        user = user or self.requester
        return TranslationRun.objects.create(client=user.client, client_name=user.email,
                                             project_id=self.source.project_id, service_id=self.source.service_id,
                                             from_language="en", to_language="fr", status="Pending")

    def translated(self, file_id, run_id, *_args):
        destination = self.path.parent / "translations" / "French" / str(run_id) / self.path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        document = Document()
        document.add_paragraph("Le contrat exige un preavis de trente jours.")
        document.save(destination)
        return {"translated_file": str(destination)}

    @mock.patch("document_translation.tasks.translation_service", new_callable=mock.MagicMock)
    def test_translation_creates_real_openable_output_once_with_source_owner_and_requester_access(self, service):
        from document_translation.tasks import translate_document_task, download_translated_file_task
        service.translate_file.side_effect = self.translated
        run = self.translation_run()
        result = translate_document_task.run(self.source.pk, run.pk, "en", "fr", requested_by_id=self.requester.pk)
        self.assertEqual(result["status"], "Completed", result)
        output = File.objects.get(pk=result["registered_outputs"][0]["file_id"])
        self.assertEqual(output.user_id, self.owner.pk)
        self.assertEqual(output.project_id, self.source.project_id)
        self.assertEqual(output.origin_file_id, self.source.pk)
        self.assertTrue(FileFolderLink.objects.filter(file=output, is_trashed=False).exists())
        self.assertIn("preavis", Document(output.filepath).paragraphs[0].text)
        self.assertEqual(TranslationFile.objects.filter(run=run).count(), 1)
        self.assertEqual(TranslationStorage.objects.filter(run=run).count(), 1)
        self.assertEqual(Storage.objects.filter(user=self.owner).count(), 1)
        download = self.client_for(self.requester).get(f"/api/v1/core/download/?file_id={output.pk}")
        self.assertEqual(download.status_code, 200)
        self.assertTrue(b"".join(download.streaming_content).startswith(b"PK"))
        self.assertEqual(self.client_for(self.foreign).get(f"/api/v1/core/download/?file_id={output.pk}").status_code, 404)
        again = translate_document_task.run(self.source.pk, run.pk, "en", "fr", requested_by_id=self.requester.pk)
        self.assertEqual(again["registered_outputs"][0]["file_id"], output.pk)
        self.assertEqual(service.translate_file.call_count, 1)
        self.assertEqual(download_translated_file_task.run(self.source.pk)["translated_filepath"], output.filepath)

    @mock.patch("document_translation.tasks.translation_service", new_callable=mock.MagicMock)
    def test_translation_rechecks_revoked_access_before_registering_output(self, service):
        from document_translation.tasks import translate_document_task
        def translate(*args):
            output = self.translated(*args)
            EffectiveAccess.objects.filter(user=self.requester, file=self.source).delete()
            return output
        service.translate_file.side_effect = translate
        run = self.translation_run()
        result = translate_document_task.run(self.source.pk, run.pk, "en", "fr", requested_by_id=self.requester.pk)
        self.assertEqual(result["status"], "Failed")
        self.assertFalse(File.objects.filter(origin_file=self.source).exists())
        self.assertFalse(TranslationFile.objects.filter(run=run, status="Completed").exists())

    @mock.patch("document_translation.tasks.translation_service", new_callable=mock.MagicMock)
    def test_foreign_actor_never_calls_translation_provider(self, service):
        from document_translation.tasks import translate_document_task
        result = translate_document_task.run(self.source.pk, self.translation_run(self.foreign).pk,
                                             "en", "fr", requested_by_id=self.foreign.pk)
        self.assertEqual(result["status"], "Failed")
        service.translate_file.assert_not_called()

    @mock.patch("document_translation.views.translate_document_task.delay")
    def test_translation_submit_allows_shared_processing_and_queues_authenticated_actor_after_commit(self, submit):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.requester).post(
                f"/api/v1/translation/submit-translation/?file_id={self.source.pk}&from_language=en&to_language=fr")
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(submit.call_args.kwargs["requested_by_id"], self.requester.pk)
        run_id = response.json()["translation_run_id"]
        status = self.client_for(self.requester).get(f"/api/v1/translation/check-translation-status/?run_id={run_id}")
        self.assertEqual(status.status_code, 200, status.content)
        self.assertEqual(status.json()["status"], "Processing")
        foreign = self.client_for(self.foreign).post(
            f"/api/v1/translation/submit-translation/?file_id={self.source.pk}&from_language=en&to_language=fr")
        self.assertEqual(foreign.status_code, 404)

    @mock.patch("document_translation.tasks.translation_service", new_callable=mock.MagicMock)
    def test_translated_download_uses_saved_bytes_and_source_acl(self, service):
        from document_translation.tasks import translate_document_task
        service.translate_file.side_effect = self.translated
        run = self.translation_run()
        self.assertEqual(translate_document_task.run(self.source.pk, run.pk, "en", "fr", requested_by_id=self.requester.pk)["status"], "Completed")
        url = f"/api/v1/translation/download-file/?file_id={self.source.pk}&file_type=translated&language=fr"
        response = self.client_for(self.requester).get(url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(b"".join(response.streaming_content).startswith(b"PK"))
        self.assertEqual(self.client_for(self.unshared).get(url).status_code, 404)

    def test_translation_service_uses_one_container_pair_and_only_successful_real_output(self):
        from document_translation.utils import TranslationService
        service = TranslationService.__new__(TranslationService)
        service.azure_service = mock.Mock()
        service.client = mock.Mock()
        service.client.begin_translation.return_value.result.return_value = [SimpleNamespace(status="Succeeded")]
        service.azure_service.generate_sas_url.side_effect = lambda name: f"https://storage.example.invalid/{name}"
        def download(_container, destination):
            output = Path(destination) / self.path.name
            document = Document()
            document.add_paragraph("Translated provider output")
            document.save(output)
        service.azure_service.download_files.side_effect = download
        run = self.translation_run()
        result = service.translate_file(self.source.pk, run.pk, "en", "fr")
        self.assertEqual(Document(result["translated_file"]).paragraphs[0].text, "Translated provider output")
        self.assertEqual(service.azure_service.ensure_container_exists.call_count, 2)
        self.assertEqual(service.azure_service.upload_file_if_not_exists.call_count, 1)
        self.assertEqual(service.azure_service.delete_container.call_count, 2)
        self.assertFalse(TranslationFile.objects.filter(run=run).exists())  # guarded task publishes

    def test_real_docx_metadata_dates_and_extended_fields_are_saved_and_readable(self):
        from core.tasks import process_metadata
        run = Run.objects.create(user=self.owner, status="Processing")
        result = process_metadata.run(self.source.pk, run.pk, requested_by_id=self.requester.pk)
        self.assertNotIn("error", result, result)
        metadata = Metadata.objects.get(file=self.source)
        self.assertEqual(metadata.title, "Contract title")
        self.assertEqual(metadata.last_modified_by, "Test editor")
        self.assertIsNotNone(metadata.creationdate.tzinfo)
        status = self.client_for(self.requester).get(f"/api/v1/core/metadata-status/?file_id={self.source.pk}")
        self.assertEqual(status.status_code, 200, status.content)
        self.assertEqual(status.json()["author"], "Test author")
        self.assertEqual(self.client_for(self.foreign).get(f"/api/v1/core/metadata-status/?file_id={self.source.pk}").status_code, 404)
        process_metadata.run(self.source.pk, run.pk, requested_by_id=self.requester.pk)
        self.assertEqual(Metadata.objects.filter(file=self.source).count(), 1)

    @mock.patch("core.tasks.process_metadata.delay")
    def test_metadata_status_tracks_exact_requested_run_and_never_returns_an_old_row(self, queue):
        from core.tasks import process_metadata
        Metadata.objects.create(file=self.source, title="Old cached metadata")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client_for(self.requester).post(f"/api/v1/core/metadata/?file_id={self.source.pk}")
        self.assertEqual(response.status_code, 202, response.content)
        run_id = response.json()["run_id"]
        url = f"/api/v1/core/metadata-status/?file_id={self.source.pk}&run_id={run_id}"
        pending = self.client_for(self.requester).get(url)
        self.assertEqual(pending.status_code, 202)
        self.assertEqual(pending.json(), {"status": "Pending", "metadata": None, "error_message": None})
        process_metadata.run(self.source.pk, run_id, requested_by_id=self.requester.pk)
        completed = self.client_for(self.requester).get(url)
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.json()["metadata"]["title"], "Contract title")
        self.assertEqual(self.client_for(self.foreign).get(url).status_code, 404)
        other = make_doc(self.owner, "other.docx", "text")
        self.assertEqual(self.client_for(self.owner).get(
            f"/api/v1/core/metadata-status/?file_id={other.pk}&run_id={run_id}").status_code, 404)

    @mock.patch("core.tasks.extract_metadata")
    def test_foreign_metadata_job_is_rejected_before_file_processing(self, extractor):
        from core.tasks import process_metadata
        run = Run.objects.create(user=self.foreign, status="Processing")
        result = process_metadata.run(self.source.pk, run.pk, requested_by_id=self.foreign.pk)
        self.assertIn("error", result)
        extractor.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, "Failed")

    def test_bulk_translation_honours_selected_languages_and_ignores_forged_identity(self):
        from core.bulk_registry import _bulk_call_adapter, _run_translation
        task = mock.Mock(__name__="translate")
        adapter = _bulk_call_adapter(task, user_id_param=False, run_creator=_run_translation,
                                     extra_params={"from_language": "auto", "to_language": "en"})
        adapter(self.source.pk, self.requester.pk, from_language="en", to_language="fr",
                user_id=self.foreign.pk, requested_by_id=self.foreign.pk, run_id="forged",
                project_id="other-tenant", file_id=999999)
        passed = task.delay.call_args.kwargs
        self.assertEqual(passed["file_id"], self.source.pk)
        self.assertEqual(passed["requested_by_id"], self.requester.pk)
        self.assertNotIn("user_id", passed)
        self.assertNotIn("run_id", passed)
        self.assertNotIn("project_id", passed)
        self.assertEqual((passed["from_language"], passed["to_language"]), ("en", "fr"))
        run = TranslationRun.objects.get(pk=passed["translation_run_id"])
        self.assertEqual((run.from_language, run.to_language), ("en", "fr"))
        self.assertEqual(run.client_id, self.org.pk)

    def test_bulk_ocr_honours_selected_option_and_authenticated_actor(self):
        from core.bulk_registry import _bulk_call_adapter, _run_ocr
        from document_ocr.models import OCRRun
        task = mock.Mock(__name__="ocr")
        adapter = _bulk_call_adapter(task, run_creator=_run_ocr, extra_params={"ocr_option": "basic"})
        adapter(self.source.pk, self.requester.pk, ocr_option="Advanced-ocr", user_id=self.foreign.pk,
                file_id=999999, run_id="forged")
        passed = task.delay.call_args.kwargs
        self.assertEqual(passed["user_id"], self.requester.pk)
        self.assertEqual(passed["file_id"], self.source.pk)
        self.assertEqual(passed["ocr_option"], "Advanced-ocr")
        self.assertEqual(OCRRun.objects.get(pk=passed["run_id"]).ocr_option, "Advanced-ocr")

    @mock.patch("document_structures.utils.partition_pdf")
    def test_pdf_structure_persists_elements_uses_fast_native_path_and_generates_report(self, partition):
        from document_structures.models import DocumentStructureRun, DocumentElement
        from document_structures.tasks import run_document_partition_task
        self.grant_structure_analysis()
        pdf_path = self.path.with_suffix(".pdf")
        with fitz.open() as pdf:
            page = pdf.new_page()
            page.insert_text((72, 72), "OK")
            pdf.save(pdf_path)
        File.objects.filter(pk=self.source.pk).update(filepath=str(pdf_path))
        partition.return_value = [SimpleNamespace(category="NarrativeText", text="OK", metadata=SimpleNamespace(page_number=1))]
        run = DocumentStructureRun.objects.create(run=self.source.run, file=self.source, user=self.requester,
                                                 partition_strategy="partition_auto")
        result = run_document_partition_task.run(str(run.pk), store_embeddings=False,
                                                project_id="forged-matter", service_id="forged-service", generate_report=True)
        run.refresh_from_db()
        self.assertEqual(run.status, "Completed")
        self.assertEqual(DocumentElement.objects.get(run=run).text, "OK")
        self.assertEqual(partition.call_args.kwargs["strategy"], "fast")
        output = File.objects.get(pk=result["report"]["file_id"])
        self.assertEqual(output.user_id, self.owner.pk)
        self.assertEqual(output.origin_file_id, self.source.pk)
        self.assertEqual(output.project_id, self.source.project_id)
        self.assertEqual(output.run_id, self.source.run_id)
        self.assertTrue(FileFolderLink.objects.filter(file=output, is_trashed=False).exists())
        self.assertIn("Document Structure Analysis", Path(output.filepath).read_text())
        download = self.client_for(self.requester).get(f"/api/v1/core/download/?file_id={output.pk}")
        self.assertEqual(download.status_code, 200)
        self.assertIn(b"Document Structure Analysis", b"".join(download.streaming_content))
        self.assertEqual(self.client_for(self.foreign).get(f"/api/v1/core/download/?file_id={output.pk}").status_code, 404)

    @mock.patch("document_structures.utils.run_partition")
    @mock.patch("document_structures.tasks.run_document_partition_task.delay")
    def test_structure_client_without_analysis_permission_is_rejected_before_dispatch_and_processing(self, queue, partition):
        from document_structures.models import DocumentStructureRun, DocumentElement
        from document_structures.tasks import run_document_partition_task
        from rest_framework.exceptions import PermissionDenied
        response = self.client_for(self.requester).post(
            f"/api/v1/document-structures/document-structure/submit/?file_id={self.source.pk}")
        self.assertEqual(response.status_code, 403, response.content)
        queue.assert_not_called()
        self.assertFalse(DocumentStructureRun.objects.filter(file=self.source).exists())
        run = DocumentStructureRun.objects.create(run=self.source.run, file=self.source, user=self.requester)
        with self.assertRaises(PermissionDenied):
            run_document_partition_task.run(str(run.pk), store_embeddings=False, generate_report=True)
        run.refresh_from_db()
        self.assertEqual(run.status, "Failed")
        partition.assert_not_called()
        self.assertFalse(DocumentElement.objects.filter(run=run).exists())
        self.assertFalse(File.objects.filter(origin_file=self.source).exists())

    def test_structure_real_text_fixture_generates_one_report_and_repeated_task_is_idempotent(self):
        from document_structures.models import DocumentStructureRun, DocumentElement
        from document_structures.tasks import run_document_partition_task
        self.grant_structure_analysis()
        path = self.path.with_suffix(".txt")
        path.write_text("Contract terms\n\nThe notice period is thirty days.\n", encoding="utf-8")
        File.objects.filter(pk=self.source.pk).update(filepath=str(path), extension="txt")
        run = DocumentStructureRun.objects.create(run=self.source.run, file=self.source, user=self.requester,
                                                 partition_strategy="partition_text")
        result = run_document_partition_task.run(str(run.pk), store_embeddings=False, generate_report=True)
        self.assertEqual(result["status"], "Completed")
        self.assertIn("thirty days", " ".join(DocumentElement.objects.filter(run=run).values_list("text", flat=True)))
        self.assertTrue(Path(File.objects.get(pk=result["report"]["file_id"]).filepath).is_file())
        run_document_partition_task.run(str(run.pk), store_embeddings=False, generate_report=True)
        self.assertEqual(File.objects.filter(origin_file=self.source).count(), 1)

    @mock.patch("document_structures.utils.run_partition")
    def test_structure_source_change_rolls_back_extracted_rows_and_report(self, partition):
        from document_structures.models import DocumentStructureRun, DocumentElement
        from document_structures.tasks import run_document_partition_task
        self.grant_structure_analysis()
        def change_source(*args):
            with self.path.open("ab") as stream:
                stream.write(b"changed during extraction")
            return [SimpleNamespace(category="Text", text="stale content", metadata=None)]
        partition.side_effect = change_source
        run = DocumentStructureRun.objects.create(run=self.source.run, file=self.source, user=self.requester)
        with self.assertRaises(ValueError):
            run_document_partition_task.run(str(run.pk), store_embeddings=False, generate_report=True)
        run.refresh_from_db()
        self.assertEqual(run.status, "Failed")
        self.assertFalse(DocumentElement.objects.filter(run=run).exists())
        self.assertFalse(File.objects.filter(origin_file=self.source).exists())

    @mock.patch("document_structures.utils.run_partition")
    def test_structure_revoked_actor_cannot_publish_report(self, partition):
        from document_structures.models import DocumentStructureRun, DocumentElement
        from document_structures.tasks import run_document_partition_task
        self.grant_structure_analysis()
        def revoke(*args):
            EffectiveAccess.objects.filter(user=self.requester, file=self.source).delete()
            return [SimpleNamespace(category="Text", text="restricted content", metadata=None)]
        partition.side_effect = revoke
        run = DocumentStructureRun.objects.create(run=self.source.run, file=self.source, user=self.requester)
        with self.assertRaises(Exception):
            run_document_partition_task.run(str(run.pk), store_embeddings=False, generate_report=True)
        self.assertFalse(DocumentElement.objects.filter(run=run).exists())
        self.assertFalse(File.objects.filter(origin_file=self.source).exists())
