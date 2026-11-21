"""Check-out locking and download watermarks: per-document locks that only the holder or an admin can
release, and stamped PDF copies when the owner's organisation asks for them (never across organisations)."""
import tempfile
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import DocumentCheckout, File, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client, OrganisationPolicy
from document_operations.models import EffectiveAccess, FileFolderLink
from document_operations.utils import get_or_create_folder_tree

SCOPE = "oauth2_provider.contrib.rest_framework.TokenHasReadWriteScope.has_permission"


def make_pdf(path, text="Original contract text"):
    import fitz
    with fitz.open() as doc:
        doc.new_page().insert_text((72, 72), text)
        doc.save(path)


class CheckoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Lock firm")
        cls.other = Client.objects.create(name="Other lock firm")
        cls.owner = make_user("owner@lock.test", cls.firm, groups=["Client"])
        cls.editor = make_user("editor@lock.test", cls.firm, groups=["Client"])
        cls.admin = make_user("admin@lock.test", cls.firm, groups=["Admin"])
        cls.outsider = make_user("outsider@otherlock.test", cls.other, groups=["Admin"])

    def setUp(self):
        cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "deal.pdf"
        make_pdf(path)
        self.folder = get_or_create_folder_tree(["Deal"], user=self.owner, project_id="Atlas", service_id="matter")
        self.doc = File.objects.create(user=self.owner, run=Run.objects.create(user=self.owner), filename="deal.pdf",
                                       filepath=str(path), project_id="Atlas", service_id="matter", md5_hash="deal")
        self.link = FileFolderLink.objects.create(file=self.doc, folder=self.folder)
        EffectiveAccess.objects.create(user=self.editor, file=self.doc, can_download=True, can_rename=True)

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def test_holder_keeps_others_out_until_check_in(self):
        r = self.api(self.owner).post(f"/api/v1/core/files/{self.doc.pk}/checkout/", {"note": "Redlining"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["by"], "owner@lock.test")
        editor = self.api(self.editor)
        self.assertEqual(editor.post(f"/api/v1/core/files/{self.doc.pk}/checkout/").status_code, 409)
        r = editor.patch(f"/api/v1/documents/files/{self.link.pk}/rename/", {"new_name": "x.pdf"}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertIn("owner@lock.test", r.json()["detail"])
        self.assertEqual(editor.post(f"/api/v1/core/files/{self.doc.pk}/checkin/").status_code, 403)
        # The holder still works on it; the folder cannot be archived from under them.
        self.assertEqual(self.api(self.editor).patch(f"/api/v1/documents/folders/{self.folder.pk}/trash/").status_code, 404)
        self.assertEqual(self.api(self.owner).post(f"/api/v1/core/files/{self.doc.pk}/checkin/").json()["checked_out"], False)
        self.assertEqual(editor.post(f"/api/v1/core/files/{self.doc.pk}/checkout/").status_code, 200)

    def test_admin_can_force_check_in_but_not_across_organisations(self):
        self.api(self.editor).post(f"/api/v1/core/files/{self.doc.pk}/checkout/")
        self.assertEqual(self.api(self.outsider).post(f"/api/v1/core/files/{self.doc.pk}/checkin/").status_code, 404)
        self.assertTrue(DocumentCheckout.objects.filter(file=self.doc).exists())
        r = self.api(self.admin).post(f"/api/v1/core/files/{self.doc.pk}/checkin/")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(DocumentCheckout.objects.exists())

    def test_viewers_cannot_check_out(self):
        viewer = make_user("viewer@lock.test", self.firm, groups=["Client"])
        EffectiveAccess.objects.create(user=viewer, file=self.doc, can_download=True)
        self.assertEqual(self.api(viewer).post(f"/api/v1/core/files/{self.doc.pk}/checkout/").status_code, 403)

    def test_menu_shows_lock_and_disables_changes(self):
        self.api(self.owner).post(f"/api/v1/core/files/{self.doc.pk}/checkout/")
        perms = self.api(self.editor).get(f"/api/v1/documents/table/{self.doc.pk}/context/").json()["permissions"]
        self.assertEqual(perms["checked_out_by"], "owner@lock.test")
        self.assertFalse(perms["rename"])
        self.assertFalse(perms["checkin"])
        self.assertTrue(self.api(self.owner).get(f"/api/v1/documents/table/{self.doc.pk}/context/").json()["permissions"]["checkin"])

    @mock.patch(SCOPE, return_value=True)
    @override_settings(ONLYOFFICE={"DOC_SERVER_URL": "https://ds.test", "DOC_SERVER_PUBLIC_URL": "https://ds.test",
                                   "JWT_SECRET": "test-secret-for-editor-config-1234567890", "DOWNLOAD_TTL": 300,
                                   "HTTP_CONNECT_TIMEOUT": 5, "HTTP_READ_TIMEOUT": 5})
    def test_editor_is_read_only_for_others_while_checked_out(self, _scope):
        self.api(self.owner).post(f"/api/v1/core/files/{self.doc.pk}/checkout/")
        r = self.api(self.editor).get(f"/api/v1/core/onlyoffice/editor-config/?file_id={self.doc.pk}").json()
        self.assertEqual(r["config"]["editorConfig"]["mode"], "view")
        self.assertIn("owner@lock.test", r["readonly_reason"])
        r = self.api(self.owner).get(f"/api/v1/core/onlyoffice/editor-config/?file_id={self.doc.pk}").json()
        self.assertEqual(r["config"]["editorConfig"]["mode"], "edit")

    # ── watermarks ───────────────────────────────────────────────────────────────────────────
    def _download(self, user):
        with mock.patch(SCOPE, return_value=True):
            r = self.api(user).get(f"/api/v1/core/download/?file_id={self.doc.pk}")
        self.assertEqual(r.status_code, 200)
        return b"".join(r.streaming_content) if r.streaming else r.content

    def _text(self, data):
        import fitz
        with fitz.open(stream=data, filetype="pdf") as doc:
            return doc[0].get_text()

    def test_downloads_are_unmarked_by_default_and_stamped_when_turned_on(self):
        self.assertNotIn("Confidential", self._text(self._download(self.editor)))
        OrganisationPolicy.objects.update_or_create(client=self.firm, defaults={"watermark_downloads": True})
        text = self._text(self._download(self.editor))
        self.assertIn("Confidential", text)
        self.assertIn("editor@lock.test", text)
        self.assertIn("Original contract text", text)
        self.assertNotIn(b"editor@lock.test", Path(self.doc.filepath).read_bytes())   # stored file untouched

    def test_admin_settings_toggle_watermarks_with_safe_values(self):
        api = self.api(self.admin)
        r = api.patch("/api/v1/auth/admin/policy/", {"watermark_downloads": "false", "watermark_text": "x" * 201}, format="json")
        self.assertEqual(r.status_code, 400)
        r = api.patch("/api/v1/auth/admin/policy/", {"watermark_downloads": "false"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(OrganisationPolicy.objects.get(client=self.firm).watermark_downloads)
        api.patch("/api/v1/auth/admin/policy/", {"watermark_downloads": True, "watermark_text": "{org} · {email}"}, format="json")
        self.assertIn("Lock firm", self._text(self._download(self.editor)))
        # Another organisation's administrator cannot change this organisation's setting.
        self.api(self.outsider).patch("/api/v1/auth/admin/policy/", {"watermark_downloads": False}, format="json")
        self.assertTrue(OrganisationPolicy.objects.get(client=self.firm).watermark_downloads)
