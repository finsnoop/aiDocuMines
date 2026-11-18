"""Native text first; OCR only for a downstream PDF need, shared across consumers."""
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import fitz
from PIL import Image
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from core.models import File
from core.tests.test_assistant import make_doc
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from document_search import utils
from document_search.text_policy import TextPreparationBusy, _fingerprint, shared_file_text


class StructureImagePolicyTests(SimpleTestCase):
    @mock.patch("document_structures.utils.partition")
    def test_analysis_of_image_requires_explicit_ocr_without_implicit_partition_ocr(self, partition):
        from document_structures.utils import run_partition
        with self.assertRaisesRegex(ValueError, "explicit OCR"):
            run_partition(SimpleNamespace(filepath="/tmp/passport.png"), "partition_auto")
        partition.assert_not_called()


class PDFTextPolicyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Client.objects.create(name="OCR text policy tests")
        cls.user = make_user("ocr-policy@example.test", cls.org, groups=["Client"])

    def setUp(self):
        cache.clear()
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def pdf(self, *, native=None, scan=False, name="source.pdf"):
        path = Path(self.directory.name) / name
        with fitz.open() as pdf:
            page = pdf.new_page()
            if native:
                page.insert_text((72, 72), native)
            if scan:
                image = Image.new("RGB", (30, 30), "white")
                data = BytesIO()
                image.save(data, format="PNG")
                image.close()
                page.insert_image(fitz.Rect(72, 72, 300, 300), stream=data.getvalue())
            pdf.save(path)
        return path

    def document(self, path):
        file = make_doc(self.user, path.name, "")
        File.objects.filter(pk=file.pk).update(filepath=str(path))
        file.refresh_from_db()
        return file

    @mock.patch("document_search.utils._ocr_pdf_for_text")
    def test_short_native_pdf_never_invokes_ocr(self, ocr):
        path = self.pdf(native="OK")
        self.assertEqual(utils.extract_text(path), "OK")
        self.assertEqual(utils.extract_text(path, allow_ocr=True), "OK")
        ocr.assert_not_called()

    @mock.patch("document_search.utils.pytesseract.image_to_string", return_value="Recovered scan text")
    def test_scanned_pdf_only_ocr_when_downstream_opts_in(self, ocr):
        path = self.pdf(scan=True)
        self.assertEqual(utils.extract_text(path), "")
        ocr.assert_not_called()
        self.assertEqual(utils.extract_text(path, allow_ocr=True), "Recovered scan text")
        ocr.assert_called_once()
        self.assertGreater(ocr.call_args.kwargs["timeout"], 0)

    @mock.patch("document_search.utils.pytesseract.image_to_string")
    def test_blank_pdf_and_image_upload_do_not_run_ocr(self, ocr):
        self.assertEqual(utils.extract_text(self.pdf(), allow_ocr=True), "")
        image = Path(self.directory.name) / "scan.png"
        Image.new("RGB", (20, 20)).save(image)
        self.assertEqual(utils.extract_text(image), "")
        self.assertEqual(utils.extract_text(image, allow_ocr=True), "")
        ocr.assert_not_called()

    @mock.patch("document_search.utils.pytesseract.image_to_string", return_value="Recovered scan text")
    def test_multiple_consumers_reuse_one_pdf_ocr_result(self, ocr):
        file = self.document(self.pdf(scan=True))
        stale_second_consumer = File.objects.get(pk=file.pk)
        self.assertEqual(shared_file_text(file, allow_ocr=True), "Recovered scan text")
        self.assertEqual(shared_file_text(stale_second_consumer, allow_ocr=True), "Recovered scan text")
        self.assertEqual(ocr.call_count, 1)
        file.refresh_from_db()
        self.assertEqual(file.content, "Recovered scan text")
        provenance = cache.get(f"text-preparation:{_fingerprint(file)}:provenance")
        self.assertEqual(provenance["method"], "ocr")
        self.assertTrue(provenance["downstream_ocr"])

    @mock.patch("document_search.utils.pytesseract.image_to_string", return_value="")
    def test_unsuccessful_ocr_is_not_repeated_by_each_indexer(self, ocr):
        file = self.document(self.pdf(scan=True))
        self.assertEqual(shared_file_text(file, allow_ocr=True), "")
        self.assertEqual(shared_file_text(file, allow_ocr=True), "")
        self.assertEqual(ocr.call_count, 1)

    @mock.patch("document_search.utils.pytesseract.image_to_string")
    def test_native_upload_preparation_and_classification_do_not_ocr(self, ocr):
        from core.tasks import extract_document_text_task
        from document_search.tasks import classify_file
        file = self.document(self.pdf(scan=True, name="invoice.pdf"))
        extract_document_text_task.run(file.pk)
        result = classify_file.run(file.pk, force=True)
        file.refresh_from_db()
        self.assertEqual(file.status, "Completed")
        self.assertEqual(file.content, "")
        self.assertEqual(result["document_type"], "Invoice")
        ocr.assert_not_called()

    @mock.patch("document_search.utils.pytesseract.image_to_string")
    def test_image_indexing_and_classification_do_not_ocr(self, ocr):
        from document_search.tasks import classify_file
        image = Path(self.directory.name) / "invoice.png"
        Image.new("RGB", (20, 20)).save(image)
        file = self.document(image)
        self.assertEqual(utils.compute_chunks(file.filepath, file=file, allow_ocr=True), ([], []))
        self.assertEqual(classify_file.run(file.pk)["document_type"], "Invoice")
        ocr.assert_not_called()

    @mock.patch("document_search.utils.pytesseract.image_to_string", return_value="Text needed for search")
    @mock.patch("document_search.utils.split_text", return_value=["Text needed for search"])
    @mock.patch("document_search.utils.embed_texts", return_value=[[0.1, 0.2]])
    def test_actual_indexing_need_can_use_conditional_pdf_ocr(self, _embed, _split, ocr):
        file = self.document(self.pdf(scan=True))
        self.assertEqual(utils.compute_chunks(file.filepath, file=file, allow_ocr=True),
                         (["Text needed for search"], [[0.1, 0.2]]))
        ocr.assert_called_once()

    @mock.patch("document_search.utils.pytesseract.image_to_string")
    def test_concurrent_preparation_does_not_launch_duplicate_ocr(self, ocr):
        file = self.document(self.pdf(scan=True))
        cache.add(f"text-preparation:{_fingerprint(file)}:lock", "other-worker", 60)
        with self.assertRaises(TextPreparationBusy):
            shared_file_text(file, allow_ocr=True)
        ocr.assert_not_called()

    @mock.patch("document_search.utils.pytesseract.image_to_string", return_value="Existing OCR text")
    def test_native_upload_pass_cannot_erase_text_prepared_for_an_index(self, ocr):
        from core.tasks import extract_document_text_task
        file = self.document(self.pdf(scan=True))
        shared_file_text(file, allow_ocr=True)
        extract_document_text_task.run(file.pk)
        file.refresh_from_db()
        self.assertEqual(file.content, "Existing OCR text")
        ocr.assert_called_once()

    @mock.patch("document_ocr.views.process_ocr.delay")
    def test_explicit_ocr_request_still_queues_for_native_text_pdf(self, submit):
        from document_ocr.views import SubmitOCRAPIView
        from rest_framework.test import APIRequestFactory, force_authenticate
        from document_ocr.models import OCRRun
        file = self.document(self.pdf(native="OK"))
        request = APIRequestFactory().post("/", {"file_id": file.pk})
        request.META["QUERY_STRING"] = f"file_id={file.pk}&ocr_option=Basic-ocr"
        force_authenticate(request, user=self.user)
        with mock.patch("oauth2_provider.contrib.rest_framework.TokenHasReadWriteScope.has_permission", return_value=True), self.captureOnCommitCallbacks(execute=True):
            response = SubmitOCRAPIView.as_view()(request)
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(OCRRun.objects.count(), 1)
        submit.assert_called_once()
