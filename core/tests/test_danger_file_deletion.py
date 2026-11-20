"""danger_file_deletion.py wipes documents from disk and database and keeps accounts and non-document media."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

import danger_file_deletion as danger
from core.models import File
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from document_operations.models import FileFolderLink, Folder
from document_operations.utils import get_or_create_folder_tree


class DangerFileDeletionTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.media = Path(self.tmp.name) / "media"
        (self.media / "uploads" / "1" / "Atlas").mkdir(parents=True)
        (self.media / "translations" / "French").mkdir(parents=True)
        (self.media / "branding").mkdir()
        (self.media / "branding" / "logo.png").write_bytes(b"logo")
        self.org = Client.objects.create(name="Danger firm")
        self.user = make_user("danger@example.test", self.org, groups=["Client"])
        settings_patch = override_settings(MEDIA_ROOT=str(self.media))
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        for target, patched in (("search_inventory", mock.Mock(return_value=({}, []))),
                                ("document_cache_keys", mock.Mock(return_value=set())),
                                ("clear_document_cache", mock.Mock())):
            p = mock.patch(f"core.document_purge.{target}", patched)
            p.start()
            self.addCleanup(p.stop)

    def make_document(self, name):
        from core.models import Run
        path = self.media / "uploads" / "1" / "Atlas" / name
        path.write_bytes(b"%PDF-1.4 test")
        f = File.objects.create(filename=name, filepath=str(path), user=self.user,
                                run=Run.objects.create(user=self.user, status="Completed"),
                                project_id="Atlas", service_id="matter", md5_hash=name)
        folder = get_or_create_folder_tree(["Contracts"], user=self.user, project_id="Atlas", service_id="matter")
        FileFolderLink.objects.filter(file=f).delete()
        FileFolderLink.objects.create(file=f, folder=folder)
        return f, path

    def test_dry_run_deletes_nothing(self):
        f, path = self.make_document("a.pdf")
        self.assertEqual(danger.main(["--dry-run"]), 0)
        self.assertTrue(path.exists())
        self.assertTrue(File.objects.filter(pk=f.pk).exists())

    def test_refuses_without_confirmation_when_not_interactive(self):
        f, path = self.make_document("a.pdf")
        with mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(danger.main([]), 1)
        self.assertTrue(path.exists())
        self.assertTrue(File.objects.filter(pk=f.pk).exists())

    def test_wipes_documents_folders_and_files_but_keeps_accounts_and_other_media(self):
        _, path = self.make_document("a.pdf")
        self.make_document("b.pdf")
        (self.media / "translations" / "French" / "a.docx").write_bytes(b"x")
        self.assertEqual(danger.main(["--confirm", danger.CONFIRM_PHRASE]), 0)
        self.assertFalse(File.objects.exists())
        self.assertFalse(FileFolderLink.objects.exists())
        self.assertFalse(Folder.objects.exists())
        self.assertFalse(path.exists())
        self.assertFalse((self.media / "translations").exists())
        self.assertTrue((self.media / "uploads").is_dir())          # recreated empty
        self.assertEqual(list((self.media / "uploads").iterdir()), [])
        self.assertTrue((self.media / "branding" / "logo.png").exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())
        self.assertTrue(Client.objects.filter(pk=self.org.pk).exists())

    def test_keep_folders(self):
        self.make_document("a.pdf")
        self.assertEqual(danger.main(["--confirm", danger.CONFIRM_PHRASE, "--keep-folders"]), 0)
        self.assertFalse(File.objects.exists())
        self.assertTrue(Folder.objects.exists())
