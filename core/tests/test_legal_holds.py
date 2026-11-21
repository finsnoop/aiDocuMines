"""Legal holds: who may place and release them, and that held documents cannot be changed or deleted
through any route (API, folder operations, editor, model code), never across organisations."""
import tempfile
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.db import transaction
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.holds import DocumentOnHold, is_held
from core.models import File, LegalHold, Matter, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from document_operations.models import EffectiveAccess, FileFolderLink, Folder
from document_operations.utils import delete_folder, get_or_create_folder_tree


class LegalHoldTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Hold firm")
        cls.other = Client.objects.create(name="Other firm")
        cls.admin = make_user("admin@hold.test", cls.firm, groups=["Admin"])
        cls.compliance = make_user("compliance@hold.test", cls.firm, groups=["ComplianceOfficer"])
        cls.owner = make_user("owner@hold.test", cls.firm, groups=["Client"])
        cls.reader = make_user("reader@hold.test", cls.firm, groups=["Client"])
        cls.outsider = make_user("outsider-admin@other.test", cls.other, groups=["Admin"])
        cls.matter = Matter.objects.create(client=cls.firm, name="Atlas litigation", created_by=cls.admin)

    def setUp(self):
        cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = get_or_create_folder_tree(["Pleadings"], user=self.owner, project_id=self.matter.name, service_id="matter")
        self.held = self.doc("claim.pdf", self.matter.name)
        self.free = self.doc("memo.pdf", "Other matter")

    def doc(self, name, project, folder=None):
        path = Path(self.tmp.name) / name
        path.write_bytes(b"%PDF-1.4 " + name.encode())
        f = File.objects.create(user=self.owner, run=Run.objects.create(user=self.owner), filename=name,
                                filepath=str(path), project_id=project, service_id="matter", md5_hash=name)
        FileFolderLink.objects.create(file=f, folder=folder or self.folder)
        return f

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def place(self, user=None, **body):
        body = {"name": "Atlas preservation", "reason": "Litigation", "matter_id": str(self.matter.id), **body}
        return self.api(user or self.compliance).post("/api/v1/core/legal-holds/", body, format="json")

    # ── who may place, see and release ───────────────────────────────────────────────────────
    def test_compliance_and_admins_place_holds_others_cannot(self):
        self.assertEqual(self.place(self.owner).status_code, 403)
        r = self.place()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(is_held(self.held))
        self.assertFalse(is_held(self.free))
        self.assertEqual(self.place(self.admin, name="Second").status_code, 201)

    def test_holds_never_cross_organisations(self):
        hold_id = self.place().json()["id"]
        self.assertEqual(self.api(self.outsider).get("/api/v1/core/legal-holds/").json()["results"], [])
        r = self.api(self.outsider).post(f"/api/v1/core/legal-holds/{hold_id}/release/", {"reason": "x"}, format="json")
        self.assertEqual(r.status_code, 404)
        # Another organisation's documents and matters cannot be put on hold.
        self.assertEqual(self.place(self.outsider).status_code, 404)
        r = self.api(self.outsider).post("/api/v1/core/legal-holds/", {"name": "x", "file_ids": [self.free.pk]}, format="json")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(is_held(self.free))

    def test_document_hold_and_release_needs_a_reason(self):
        r = self.place(matter_id=None, file_ids=[self.free.pk])
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(is_held(self.free))
        self.assertFalse(is_held(self.held))
        url = f"/api/v1/core/legal-holds/{r.json()['id']}/release/"
        self.assertEqual(self.api(self.owner).post(url, {"reason": "done"}, format="json").status_code, 403)
        self.assertEqual(self.api(self.compliance).post(url, {}, format="json").status_code, 400)
        self.assertEqual(self.api(self.compliance).post(url, {"reason": "Case settled"}, format="json").status_code, 200)
        self.assertFalse(is_held(self.free))
        self.assertTrue(LegalHold.objects.filter(pk=r.json()["id"]).exists())  # kept as a record

    # ── enforcement through the API ──────────────────────────────────────────────────────────
    def test_held_document_cannot_be_deleted_archived_or_renamed_even_by_admins(self):
        self.place()
        link = FileFolderLink.objects.get(file=self.held)
        for user in (self.owner, self.admin):
            api = self.api(user)
            r = api.delete(f"/api/v1/documents/files/{link.pk}/delete/")
            self.assertEqual(r.status_code, 403, r.content)
            self.assertIn("legal hold", r.json()["detail"])
            self.assertEqual(api.post("/api/v1/documents/files/trash/", {"file_ids": [link.pk]}, format="json").status_code, 403)
            self.assertEqual(api.patch(f"/api/v1/documents/files/{link.pk}/rename/", {"new_name": "x.pdf"}, format="json").status_code, 403)
        self.held.refresh_from_db()
        self.assertEqual(self.held.filename, "claim.pdf")
        self.assertTrue(Path(self.held.filepath).exists())

    def test_folder_with_a_held_document_cannot_be_archived_or_deleted(self):
        self.place()
        api = self.api(self.owner)
        self.assertEqual(api.patch(f"/api/v1/documents/folders/{self.folder.pk}/trash/").status_code, 403)
        self.assertEqual(api.delete(f"/api/v1/documents/folders/{self.folder.pk}/delete/").status_code, 403)
        self.assertTrue(Folder.objects.filter(pk=self.folder.pk, is_trashed=False).exists())
        self.assertEqual(FileFolderLink.objects.filter(folder=self.folder).count(), 2)   # nothing removed

    def test_documents_outside_the_hold_are_unaffected(self):
        self.place()
        other_folder = get_or_create_folder_tree(["Notes"], user=self.owner, project_id="Other matter", service_id="matter")
        FileFolderLink.objects.filter(file=self.free).update(folder=other_folder)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.api(self.owner).delete(f"/api/v1/documents/folders/{other_folder.pk}/delete/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(File.objects.filter(pk=self.free.pk).exists())

    def test_release_lets_deletion_proceed(self):
        hold_id = self.place().json()["id"]
        self.api(self.compliance).post(f"/api/v1/core/legal-holds/{hold_id}/release/", {"reason": "Settled"}, format="json")
        with self.captureOnCommitCallbacks(execute=True):
            r = self.api(self.owner).delete(f"/api/v1/documents/folders/{self.folder.pk}/delete/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(File.objects.filter(pk=self.held.pk).exists())

    def test_hold_covers_documents_added_to_the_matter_later(self):
        self.place()
        later = self.doc("reply.pdf", self.matter.name)
        self.assertTrue(is_held(later))

    # ── the model guards stop every other code path ──────────────────────────────────────────
    def test_model_level_guards(self):
        self.place()

        def refused(action):
            with self.assertRaises(DocumentOnHold), transaction.atomic():
                action()

        refused(lambda: File.objects.get(pk=self.held.pk).delete())
        refused(lambda: delete_folder(self.folder.pk, actor=None))

        def rename():
            doc = File.objects.get(pk=self.held.pk)
            doc.filename = "renamed.pdf"
            doc.save()
        refused(rename)
        refused(lambda: FileFolderLink.objects.get(file=self.held).delete())

        def archive_link():
            link = FileFolderLink.objects.get(file=self.held)
            link.is_trashed = True
            link.save()
        refused(archive_link)
        # Non-identity fields (text extraction, classification) still update.
        doc = File.objects.get(pk=self.held.pk)
        doc.document_type = "pleading"
        doc.save()
        self.assertEqual(File.objects.get(pk=self.held.pk).filename, "claim.pdf")

    def test_purge_refuses_while_holds_are_active(self):
        from io import StringIO
        from django.core.management import CommandError, call_command
        self.place()
        out = StringIO()
        with mock.patch("core.document_purge.search_inventory", return_value=({}, [])), \
                mock.patch("core.document_purge.document_cache_keys", return_value=set()), \
                override_settings(MEDIA_ROOT=self.tmp.name), self.assertRaises(CommandError):
            call_command("purge_documents", "--execute", "--all-tenants", stdout=out)
        self.assertIn("legal hold", out.getvalue())
        self.assertTrue(File.objects.filter(pk=self.held.pk).exists())

    # ── editor: read-only when held or without write access ──────────────────────────────────
    @override_settings(ONLYOFFICE={"DOC_SERVER_URL": "https://ds.test", "DOC_SERVER_PUBLIC_URL": "https://ds.test",
                                   "JWT_SECRET": "test-secret-for-editor-config-1234567890", "DOWNLOAD_TTL": 300,
                                   "HTTP_CONNECT_TIMEOUT": 5, "HTTP_READ_TIMEOUT": 5})
    @mock.patch("oauth2_provider.contrib.rest_framework.TokenHasReadWriteScope.has_permission", return_value=True)
    def test_editor_opens_read_only_for_viewers_and_held_documents(self, _scope):
        EffectiveAccess.objects.create(user=self.reader, file=self.free, can_download=True)
        url = "/api/v1/core/onlyoffice/editor-config/?file_id={}"
        owner = self.api(self.owner).get(url.format(self.free.pk)).json()
        self.assertTrue(owner["can_edit"])
        self.assertEqual(owner["config"]["editorConfig"]["mode"], "edit")
        reader = self.api(self.reader).get(url.format(self.free.pk)).json()
        self.assertFalse(reader["can_edit"])
        self.assertEqual(reader["config"]["editorConfig"]["mode"], "view")
        self.assertNotIn("callbackUrl", reader["config"]["editorConfig"])
        self.place()
        held = self.api(self.owner).get(url.format(self.held.pk)).json()
        self.assertEqual(held["config"]["editorConfig"]["mode"], "view")
        self.assertIn("legal hold", held["readonly_reason"])

    def test_editor_save_task_never_overwrites_a_held_document(self):
        from core.onlyoffice_tasks import onlyoffice_fetch_and_save
        self.place()
        with mock.patch("requests.Session.get") as fetch:
            result = onlyoffice_fetch_and_save.run(self.held.pk, "https://ds.test/file")
        fetch.assert_not_called()
        self.assertEqual(result["reason"], "legal_hold")

    def test_table_and_menu_show_the_hold(self):
        self.place()
        rows = self.api(self.owner).get("/api/v1/documents/table/?page_size=100").json()["results"]
        self.assertEqual({r["filename"]: r["on_hold"] for r in rows}, {"claim.pdf": True, "memo.pdf": False})
        ctx = self.api(self.owner).get(f"/api/v1/documents/table/{self.held.pk}/context/").json()
        self.assertTrue(ctx["permissions"]["legal_hold"])
        self.assertFalse(ctx["permissions"]["delete"])
        self.assertFalse(ctx["permissions"]["rename"])
