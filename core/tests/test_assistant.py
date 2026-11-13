"""
Context assistant: answers only from documents the caller may read, never leaks
across organisations or to non-shared colleagues, and only the question goes to
the web. The model and web provider are mocked.
"""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.assistant import rank_passages
from core.models import File, Matter, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from document_operations.models import FileFolderLink
from document_operations.utils import get_or_create_folder_tree

URL = "/api/v1/core/assistant/ask/"


def make_doc(owner, name, text, project="Atlas", service="matter"):
    run = Run.objects.create(user=owner, status="Completed")
    return File.objects.create(filename=name, filepath=f"/x/{owner.id}/{name}", user=owner, run=run,
                               project_id=project, service_id=service, md5_hash=f"{owner.id}-{name}", content=text)


def ask(user, context, question="What is the notice period?", **extra):
    c = APIClient()
    c.force_authenticate(user=user)
    return c.post(URL, {"context": context, "question": question, **extra}, format="json")


@mock.patch("grid_documents_interrogation.utils.dispatch_to_llm", return_value="Thirty days [S1].")
class AssistantAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Firm A")
        cls.other_firm = Client.objects.create(name="Firm B")
        cls.alice = make_user("alice@firm-a.test", cls.firm, groups=["Client"])
        cls.bob = make_user("bob@firm-a.test", cls.firm, groups=["Client"])          # same firm, nothing shared
        cls.eve = make_user("eve@firm-b.test", cls.other_firm, groups=["Client"])    # other firm
        cls.guest = make_user("guest@firm-a.test", cls.firm, groups=["Guest"])
        cls.doc = make_doc(cls.alice, "nda.pdf", "Either party may terminate on thirty days written notice.")
        cls.secret = make_doc(cls.eve, "secret.pdf", "Atlas acquisition price is 900 million.", project="Atlas")
        cls.folder = get_or_create_folder_tree(["Contracts"], user=cls.alice, project_id="Atlas", service_id="matter")
        FileFolderLink.objects.create(file=cls.doc, folder=cls.folder)

    def setUp(self):
        cache.clear()

    def test_owner_gets_cited_answer_from_their_file(self, llm):
        r = ask(self.alice, {"type": "file", "id": self.doc.id})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["sources"][0]["filename"], "nda.pdf")
        self.assertIn("thirty days", llm.call_args[0][1])          # context block sent to the model

    def test_other_firm_and_unshared_colleague_get_404(self, llm):
        for user in (self.eve, self.bob):
            self.assertEqual(ask(user, {"type": "file", "id": self.doc.id}).status_code, 404, user.email)
            self.assertEqual(ask(user, {"type": "folder", "id": str(self.folder.id)}).status_code, 404, user.email)
        llm.assert_not_called()

    def test_project_context_only_contains_readable_documents(self, llm):
        r = ask(self.alice, {"type": "project", "project_id": "Atlas"})
        self.assertEqual(r.status_code, 200, r.content)
        names = {s["filename"] for s in r.json()["sources"]}
        self.assertNotIn("secret.pdf", names)                       # same project name, other firm
        self.assertNotIn("900 million", llm.call_args[0][1])

    def test_matter_requires_team_membership(self, llm):
        m = Matter.objects.create(client=self.firm, name="Atlas", created_by=self.alice)
        m.members.add(self.alice)
        self.assertEqual(ask(self.bob, {"type": "matter", "id": str(m.id)}).status_code, 404)
        m.members.add(self.bob)
        r = ask(self.bob, {"type": "matter", "id": str(m.id)})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([s["filename"] for s in r.json()["sources"]], ["nda.pdf"])

    def test_guest_role_cannot_use_the_assistant(self, llm):
        doc = make_doc(self.guest, "g.pdf", "text")
        self.assertEqual(ask(self.guest, {"type": "file", "id": doc.id}).status_code, 403)

    def test_unknown_context_is_404(self, llm):
        self.assertEqual(ask(self.alice, {"type": "folder", "id": "not-a-uuid"}).status_code, 404)
        self.assertEqual(ask(self.alice, {"type": "file", "id": 999999}).status_code, 404)


@mock.patch("grid_documents_interrogation.utils.dispatch_to_llm", return_value="Answer [W1].")
class AssistantWebTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Firm A")
        cls.alice = make_user("alice@firm-a.test", cls.firm, groups=["Client"])
        cls.doc = make_doc(cls.alice, "Project Falcon term sheet.pdf", "Confidential: price 450 million.")

    def setUp(self):
        cache.clear()

    def test_web_off_by_default(self, llm):
        r = ask(self.alice, {"type": "file", "id": self.doc.id})
        self.assertEqual(r.json()["web"], "off")

    @mock.patch.dict("os.environ", {"WEB_SEARCH_PROVIDER": "", "WEB_SEARCH_API_KEY": ""})
    def test_web_not_configured(self, llm):
        self.assertEqual(ask(self.alice, {"type": "file", "id": self.doc.id}, web=True).json()["web"], "not_configured")

    @mock.patch.dict("os.environ", {"WEB_SEARCH_PROVIDER": "tavily", "WEB_SEARCH_API_KEY": "k", "ASSISTANT_WEB_ENABLED": "true"})
    @mock.patch("core.web_search.requests.post")
    def test_only_the_question_is_sent_to_the_web(self, post, llm):
        post.return_value = mock.Mock(status_code=200, raise_for_status=lambda: None,
                                      json=lambda: {"results": [{"title": "T", "url": "https://e.x", "content": "c"}]})
        r = ask(self.alice, {"type": "file", "id": self.doc.id}, question="What is a typical break fee?", web=True)
        self.assertEqual(r.json()["web"], "used")
        sent = str(post.call_args)
        self.assertIn("What is a typical break fee?", sent)
        for private in ("450 million", "Falcon", "term sheet"):
            self.assertNotIn(private, sent)

    @mock.patch.dict("os.environ", {"WEB_SEARCH_PROVIDER": "tavily", "WEB_SEARCH_API_KEY": "k", "ASSISTANT_WEB_ENABLED": "false"})
    def test_platform_switch_disables_web(self, llm):
        self.assertEqual(ask(self.alice, {"type": "file", "id": self.doc.id}, web=True).json()["web"], "disabled")


class RankingTests(TestCase):
    def test_relevant_passage_ranks_first(self):
        ranked = rank_passages("notice period for termination", [
            ("a", "The parties are Alpha Ltd and Beta Ltd."),
            ("b", "Termination: either party may terminate on 30 days notice."),
            ("c", "Governing law is South Africa."),
        ])
        self.assertEqual(ranked[0][0], "b")
