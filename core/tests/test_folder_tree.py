"""The folder tree must show <matter>/<service>/<date or sub-folders>, never server storage paths."""
from django.test import TestCase

from core.models import File, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from document_operations.models import FileFolderLink, Folder
from document_operations.utils import (
    folder_parts_for, get_or_create_folder_tree, register_file_folder_link, repair_misplaced_folder_links,
)


def make(owner, name, project, service, path):
    run = Run.objects.create(user=owner, status="Completed")
    return File.objects.create(filename=name, filepath=path, user=owner, run=run,
                               project_id=project, service_id=service, md5_hash=f"{owner.id}-{name}-{project}")


class FolderTreeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Firm A")
        cls.user = make_user("a1@firm-a.test", cls.firm, groups=["Client"])

    def test_sub_folders_after_matter_and_service(self):
        f = make(self.user, "spa.pdf", "Project Atlas", "due-diligence",
                 "/app/media/demo/4/Project Atlas/due-diligence/Commercial contracts/spa.pdf")
        self.assertEqual(folder_parts_for(f), ["Commercial contracts"])

    def test_copy_from_another_matter_uses_date_not_storage_path(self):
        f = make(self.user, "brief.pdf", "Matter B", "matter",
                 "/app/media/uploads/2/2/Matter A/matter/20261113/063713_brief.pdf")
        parts = folder_parts_for(f)
        self.assertEqual(len(parts), 1)
        self.assertRegex(parts[0], r"^\d{8}$")
        register_file_folder_link(f)
        names = []
        folder = FileFolderLink.objects.get(file=f).folder
        while folder:
            names.append(folder.name)
            folder = folder.parent
        self.assertNotIn("app", names)
        self.assertNotIn("uploads", names)

    def test_repair_moves_files_out_of_storage_folders(self):
        f = make(self.user, "old.pdf", "Matter C", "matter", "/app/media/uploads/9/9/Elsewhere/x/old.pdf")
        bad = get_or_create_folder_tree(["", "app", "media", "uploads", "9"], user=self.user,
                                        project_id="Matter C", service_id="matter")
        FileFolderLink.objects.create(file=f, folder=bad)
        moved, deleted = repair_misplaced_folder_links()
        self.assertEqual(moved, 1)
        self.assertGreaterEqual(deleted, 1)
        self.assertFalse(Folder.objects.filter(name="app").exists())
        self.assertRegex(FileFolderLink.objects.get(file=f).folder.name, r"^\d{8}$")
        self.assertEqual(repair_misplaced_folder_links(), (0, 0))  # idempotent


class DocumentTableTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Firm A")
        cls.other = Client.objects.create(name="Firm B")
        cls.user = make_user("a1@firm-a.test", cls.firm, groups=["Client"])
        cls.stranger = make_user("b1@firm-b.test", cls.other, groups=["Client"])
        for i in range(7):
            make(cls.user, f"doc{i}.pdf", "Atlas" if i % 2 else "Orion", "matter", f"/x/doc{i}.pdf")
        make(cls.stranger, "secret.pdf", "Atlas", "matter", "/y/secret.pdf")

    def _get(self, user, qs=""):
        from rest_framework.test import APIClient
        c = APIClient()
        c.force_authenticate(user=user)
        return c.get("/api/v1/documents/table/" + qs).json()

    def test_paginates_and_counts(self):
        r = self._get(self.user, "?page_size=3")
        self.assertEqual((r["count"], len(r["results"]), r["has_next"]), (7, 3, True))
        r = self._get(self.user, "?page=3&page_size=3")
        self.assertEqual((len(r["results"]), r["has_next"]), (1, False))

    def test_filters_sort_and_isolation(self):
        names = [d["filename"] for d in self._get(self.user, "?project_id=Atlas&ordering=name")["results"]]
        self.assertEqual(names, ["doc1.pdf", "doc3.pdf", "doc5.pdf"])
        self.assertNotIn("secret.pdf", [d["filename"] for d in self._get(self.user, "?page_size=100")["results"]])

    def test_folder_filter_includes_sub_folders_and_stays_isolated(self):
        """A folder chosen in the Locations tree filters All documents to it and every folder below it."""
        top = get_or_create_folder_tree(["Contracts"], user=self.user, project_id="Atlas", service_id="matter")
        sub = get_or_create_folder_tree(["Contracts", "Signed"], user=self.user, project_id="Atlas", service_id="matter")
        other = get_or_create_folder_tree(["Invoices"], user=self.user, project_id="Atlas", service_id="matter")
        a = make(self.user, "msa.pdf", "Atlas", "matter", "/x/msa.pdf")
        b = make(self.user, "nda-signed.pdf", "Atlas", "matter", "/x/nda-signed.pdf")
        c = make(self.user, "inv.pdf", "Atlas", "matter", "/x/inv.pdf")
        for f, folder in ((a, top), (b, sub), (c, other)):
            FileFolderLink.objects.filter(file=f).delete()
            FileFolderLink.objects.create(file=f, folder=folder)
        names = sorted(d["filename"] for d in self._get(self.user, f"?folder={top.id}&page_size=100")["results"])
        self.assertEqual(names, ["msa.pdf", "nda-signed.pdf"])
        # someone else's folder id reveals nothing, not even that the folder exists
        theirs = get_or_create_folder_tree(["Private"], user=self.stranger, project_id="Atlas", service_id="matter")
        from rest_framework.test import APIClient
        c2 = APIClient()
        c2.force_authenticate(user=self.user)
        self.assertEqual(c2.get(f"/api/v1/documents/table/?folder={theirs.id}").status_code, 404)
        self.assertEqual(c2.get("/api/v1/documents/table/?folder=not-a-uuid").status_code, 400)

    def test_facets(self):
        from rest_framework.test import APIClient
        c = APIClient()
        c.force_authenticate(user=self.user)
        f = c.get("/api/v1/documents/table/facets/").json()
        self.assertEqual(f["matters"], ["Atlas", "Orion"])
