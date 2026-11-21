"""CLIENT CONTENT POLICY (core/access.py): platform superusers administer the platform but never see another
organisation's content: documents, folders, matters, contracts, processing records or AI answers. For content they
are an administrator of their own organisation. Platform administration across organisations still works."""
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from core.access import accessible_files, can_access_file, can_access_folder, matters_for_user
from core.models import File, Matter, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client

SCOPE = "oauth2_provider.contrib.rest_framework.TokenHasReadWriteScope.has_permission"


class SuperuserContentPolicyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.platform_org = Client.objects.create(name="AI DocuMines")
        cls.client_firm = Client.objects.create(name="Client firm")
        cls.root = make_user("root@platform.test", cls.platform_org, superuser=True)
        cls.lawyer = make_user("lawyer@client.test", cls.client_firm, groups=["Manager"])
        cls.colleague = make_user("colleague@platform.test", cls.platform_org, groups=["Client"])

    def setUp(self):
        self.client_doc = File.objects.create(user=self.lawyer, run=Run.objects.create(user=self.lawyer),
                                              filename="client-secret.pdf", filepath="/x/c.pdf", project_id="Deal",
                                              service_id="matter", md5_hash="c")
        self.own_doc = File.objects.create(user=self.colleague, run=Run.objects.create(user=self.colleague),
                                           filename="platform-note.pdf", filepath="/x/p.pdf", project_id="Ops",
                                           service_id="matter", md5_hash="p")
        self.client_matter = Matter.objects.create(client=self.client_firm, name="Deal", created_by=self.lawyer)
        self.own_matter = Matter.objects.create(client=self.platform_org, name="Ops", created_by=self.colleague)

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def test_no_client_documents_matters_or_folders(self):
        visible = set(accessible_files(self.root).values_list("filename", flat=True))
        self.assertEqual(visible, {"platform-note.pdf"})                    # own organisation only
        self.assertFalse(can_access_file(self.root, self.client_doc))
        self.assertTrue(can_access_file(self.root, self.own_doc))
        self.assertEqual(list(matters_for_user(self.root).values_list("name", flat=True)), ["Ops"])
        from document_operations.utils import get_or_create_folder_tree
        folder = get_or_create_folder_tree(["Deal"], user=self.lawyer, project_id="Deal", service_id="matter")
        self.assertFalse(can_access_folder(self.root, folder))

    def test_document_pages_and_contracts_hide_client_content(self):
        from contracts.models import Contract
        Contract.objects.create(client=self.client_firm, title="Client MSA", owner=self.lawyer)
        Contract.objects.create(client=self.platform_org, title="Our reseller agreement", owner=self.colleague)
        with mock.patch(SCOPE, return_value=True):
            table = self.api(self.root).get("/api/v1/documents/table/").json()
            matters = self.api(self.root).get("/api/v1/core/projects/").json()
            contracts = self.api(self.root).get("/api/v1/contracts/contracts/").json()
        self.assertEqual([r["filename"] for r in table["results"]], ["platform-note.pdf"])
        self.assertNotIn("Deal", str(matters))
        titles = [c["title"] for c in contracts.get("results", [])]
        self.assertIn("Our reseller agreement", titles)
        self.assertNotIn("Client MSA", titles)
        with mock.patch(SCOPE, return_value=True):
            self.assertEqual(self.api(self.root).get(f"/api/v1/core/download/?file_id={self.client_doc.pk}").status_code, 404)

    def test_platform_administration_across_organisations_still_works(self):
        c = self.api(self.root)
        people = c.get("/api/v1/auth/admin/people/", {"organisation": self.client_firm.pk})
        self.assertEqual(people.status_code, 200, people.content)
        self.assertIn("lawyer@client.test", str(people.content))
        orgs = c.get("/api/v1/auth/admin/organisations/")
        self.assertEqual(orgs.status_code, 200)
        self.assertIn("Client firm", str(orgs.content))
        # The client firm's own people still see their content.
        self.assertTrue(can_access_file(self.lawyer, self.client_doc))
