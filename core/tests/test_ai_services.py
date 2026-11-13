"""
Practice-area AI helpers: real services, only the caller's readable documents,
and no worker blocking on another worker. Model, search and Presidio are mocked.
"""
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

from core import ai_services
from core.models import File, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client


def doc(owner, name, text):
    run = Run.objects.create(user=owner, status="Completed")
    return File.objects.create(filename=name, filepath=f"/x/{owner.id}/{name}", user=owner, run=run,
                               project_id="P", service_id="S", md5_hash=f"{owner.id}-{name}", content=text)


class AIServicesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = make_user("alice@a.test", Client.objects.create(name="A"), groups=["Client"])
        cls.eve = make_user("eve@b.test", Client.objects.create(name="B"), groups=["Client"])
        cls.mine = doc(cls.alice, "policy.pdf", "Overtime is paid at 1.5x after 40 hours.")
        cls.theirs = doc(cls.eve, "secret.pdf", "Board minutes: acquisition at 900 million.")

    def test_pipelines_now_use_the_real_services(self):
        import ip_litigation.tasks as ip
        import labor_employment.tasks as le
        import private_equity.tasks as pe
        import regulatory_compliance.tasks as rc
        for mod in (le, rc, ip, pe):
            self.assertTrue(mod.AI_SERVICES_AVAILABLE, mod.__name__)

    @mock.patch("grid_documents_interrogation.utils.dispatch_to_llm", return_value="Paid at 1.5x.")
    @mock.patch("grid_documents_interrogation.utils._get_or_build_cached_text_for_file_id",
                side_effect=lambda fid: File.objects.get(pk=fid).content)
    def test_questions_only_read_the_users_documents(self, _text, llm):
        r = ai_services.ask_question_task.delay(query="Overtime rate?", file_ids=[self.mine.id, self.theirs.id],
                                                user_id=self.alice.id).get(timeout=5)
        self.assertEqual(r["answer"], "Paid at 1.5x.")
        self.assertEqual(r["file_ids"], [self.mine.id])
        context = llm.call_args[0][1]
        self.assertIn("Overtime", context)
        self.assertNotIn("900 million", context)

    def test_question_with_no_readable_documents_does_not_call_the_model(self):
        with mock.patch("grid_documents_interrogation.utils.dispatch_to_llm") as llm:
            r = ai_services.ask_question(query="q", file_ids=[self.theirs.id], user_id=self.alice.id)
        self.assertEqual(r["answer"], "")
        llm.assert_not_called()

    @mock.patch("document_search.tasks.exec_search", return_value=[{"file_id": 1, "text": "x"}])
    def test_semantic_search_wraps_permission_filtered_search(self, search):
        r = ai_services.semantic_search_task.delay(query="overtime", top_k=500, user_id=self.alice.id).get()
        self.assertEqual(r["results"], [{"file_id": 1, "text": "x"}])
        self.assertEqual(search.call_args[0][0], self.alice.id)
        self.assertEqual(search.call_args[0][3], 50)          # capped

    def test_pii_detection_uses_presidio_and_threshold(self):
        hits = [SimpleNamespace(entity_type="PERSON", start=0, end=8, score=0.95),
                SimpleNamespace(entity_type="DATE", start=10, end=14, score=0.4)]
        service = SimpleNamespace(analyzer=SimpleNamespace(analyze=lambda **kw: hits),
                                  _get_presidio_pii_entities=lambda: ["PERSON", "DATE"])
        with mock.patch("document_anonymizer.utils.get_shared_anonymization_service", return_value=service), \
                mock.patch("grid_documents_interrogation.utils._get_or_build_cached_text_for_file_id",
                           return_value="Jane Doe, 2024 payroll"):
            r = ai_services.detect_pii_task.delay(file_id=self.mine.id, detection_threshold=0.8).get()
        self.assertEqual([d["type"] for d in r["detections"]], ["PERSON"])
        self.assertEqual(r["detections"][0]["text"], "Jane Doe")

    def test_pii_detection_respects_access_when_user_given(self):
        with mock.patch("document_anonymizer.utils.get_shared_anonymization_service") as svc:
            r = ai_services.detect_pii(file_id=self.theirs.id, user_id=self.alice.id)
        self.assertEqual(r["detections"], [])
        svc.assert_not_called()
