"""Archiving a folder and then deleting it permanently removes its documents everywhere, disk included."""
import contextlib
import tempfile
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import File, Matter
from core.tests.test_tenancy import make_user, token_for
from custom_authentication.models import Client
from document_operations.models import FileFolderLink, Folder


class ArchivePermanentDeleteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Archive firm")
        cls.owner = make_user("owner@archive.test", cls.firm, groups=["Client"])
        cls.token = token_for(cls.owner)[1]

    def setUp(self):
        cache.clear()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.media = stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(override_settings(MEDIA_ROOT=self.media))
        for target in ["core.views.extract_document_text_task.delay", "core.views.classify_file.delay",
                       "core.views.index_file.apply_async", "core.views.generate_insights_for_user.delay",
                       "core.views.process_bulk_metadata.delay"]:
            stack.enter_context(mock.patch(target))

    def api(self):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION="Bearer " + self.token.token, HTTP_X_CLIENT_ID=self.token.application.client_id)
        return c

    def upload(self, matter, *names):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.api().post(f"/api/v1/core/projects/{matter.id}/upload/", {
                "file": [SimpleUploadedFile(n, f"%PDF-1.4 {n}".encode(), content_type="application/pdf") for n in names]
            }, format="multipart")
        self.assertEqual(r.status_code, 201, r.content)
        return list(File.objects.filter(project_id=matter.name))

    def table_names(self):
        return sorted(d["filename"] for d in self.api().get("/api/v1/documents/table/?page_size=100").json()["results"])

    def test_archive_then_delete_removes_documents_rows_and_files(self):
        matter = Matter.objects.create(client=self.firm, name="Archive matter", created_by=self.owner)
        matter.members.add(self.owner)
        files = self.upload(matter, "a.pdf", "b.pdf")
        paths = [Path(f.filepath) for f in files]
        self.assertTrue(all(p.is_file() for p in paths))
        self.assertEqual(self.table_names(), ["a.pdf", "b.pdf"])

        folders = list(Folder.objects.filter(user=self.owner, project_id=matter.name, parent=None))
        self.assertTrue(folders)
        for folder in folders:
            with self.captureOnCommitCallbacks(execute=True):
                r = self.api().patch(f"/api/v1/documents/folders/{folder.id}/trash/")
            self.assertIn(r.status_code, (200, 202), r.content)
        for folder in folders:
            with self.captureOnCommitCallbacks(execute=True):
                r = self.api().delete(f"/api/v1/documents/folders/{folder.id}/delete/")
            self.assertEqual(r.status_code, 200, r.content)

        self.assertFalse(Folder.objects.filter(pk__in=[f.pk for f in folders]).exists())
        self.assertFalse(FileFolderLink.objects.filter(file__in=files).exists())
        self.assertFalse(File.objects.filter(pk__in=[f.pk for f in files]).exists())
        self.assertFalse(any(p.exists() for p in paths))
        self.assertEqual(self.table_names(), [])

    def test_archiving_a_parent_folder_hides_documents_in_sub_folders(self):
        from document_operations.utils import get_or_create_folder_tree
        matter = Matter.objects.create(client=self.firm, name="Nested matter", created_by=self.owner)
        matter.members.add(self.owner)
        (doc,) = self.upload(matter, "nested.pdf")
        sub = get_or_create_folder_tree(["Contracts", "Signed"], user=self.owner, project_id=matter.name, service_id="matter")
        FileFolderLink.objects.filter(file=doc).update(folder=sub)
        r = self.api().patch(f"/api/v1/documents/folders/{sub.parent_id}/trash/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.table_names(), [])
        r = self.api().delete(f"/api/v1/documents/folders/{sub.parent_id}/delete/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(File.objects.filter(pk=doc.pk).exists())

    def test_delete_is_all_or_nothing_when_a_document_belongs_to_someone_else(self):
        from document_operations.utils import delete_folder, get_or_create_folder_tree
        from core.models import Run
        from rest_framework.exceptions import PermissionDenied
        colleague = make_user("colleague@archive.test", self.firm, groups=["Client"])
        folder = get_or_create_folder_tree(["Shared"], user=self.owner, project_id="M", service_id="matter")
        mine = File.objects.create(user=self.owner, run=Run.objects.create(user=self.owner), filename="mine.pdf",
                                   filepath="/nonexistent/mine.pdf", project_id="M", service_id="matter")
        theirs = File.objects.create(user=colleague, run=Run.objects.create(user=colleague), filename="theirs.pdf",
                                     filepath="/nonexistent/theirs.pdf", project_id="M", service_id="matter")
        for f in (mine, theirs):
            FileFolderLink.objects.create(file=f, folder=folder)
        stranger = make_user("member@archive.test", self.firm, groups=["Client"])
        with self.assertRaises(PermissionDenied):
            delete_folder(folder.pk, actor=stranger)
        self.assertEqual(FileFolderLink.objects.filter(folder=folder).count(), 2)  # nothing orphaned
        delete_folder(folder.pk, actor=None)
        self.assertFalse(File.objects.filter(pk__in=[mine.pk, theirs.pk]).exists())

    def test_leftovers_report_explains_unlinked_and_copies(self):
        from io import StringIO
        from django.core.management import call_command
        from core.models import Run
        run = Run.objects.create(user=self.owner)
        File.objects.create(user=self.owner, run=run, filename="legacy.pdf", filepath="/x/legacy.pdf", project_id="Old", service_id="s")
        out = StringIO()
        call_command("document_leftovers", "--email", self.owner.email, stdout=out)
        self.assertIn("unlinked     1", out.getvalue())
        self.assertIn("legacy.pdf", out.getvalue())
