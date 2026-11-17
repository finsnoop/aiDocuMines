"""Saved editor bytes must invalidate the text and fingerprint used by the Assistant."""
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from importlib import import_module
from django.apps import apps
from django.test import TestCase
from core.tests.test_assistant import make_doc
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from core.onlyoffice_tasks import onlyoffice_fetch_and_save


class SavedDocumentFreshnessTests(TestCase):
    def setUp(self):
        self.client_org = Client.objects.create(name='Saved document QA')
        self.user = make_user('saved-edit@qa.test', self.client_org)

    def test_onlyoffice_save_invalidates_old_extracted_text_and_updates_revision(self):
        file = make_doc(self.user, 'edited.docx', 'Old, already extracted contents')
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'edited.docx'
            path.write_bytes(b'old saved bytes')
            file.filepath = str(path)
            file.save(update_fields=['filepath'])
            previous_time = file.updated_at
            with patch('core.onlyoffice_tasks.requests.Session') as Session:
                Session.return_value.get.return_value.iter_content.return_value = [b'new saved bytes']
                result = onlyoffice_fetch_and_save.run(file.pk, 'https://document-server.invalid/edited')
            file.refresh_from_db()
            self.assertTrue(result['ok'])
            self.assertEqual(path.read_bytes(), b'new saved bytes')
            self.assertEqual(file.content, '')
            self.assertEqual(file.md5_hash, hashlib.md5(b'new saved bytes').hexdigest())
            self.assertGreater(file.updated_at, previous_time)

    def test_old_extraction_cannot_overwrite_a_concurrent_editor_save(self):
        from core.assistant import file_text
        from core.models import File
        from django.utils import timezone
        file = make_doc(self.user, 'race.docx', '')
        def extract(_path):
            File.objects.filter(pk=file.pk).update(md5_hash=hashlib.md5(b'new').hexdigest(),updated_at=timezone.now(),content='')
            return 'Text from bytes that were replaced during extraction'
        with patch('document_search.utils.extract_text',side_effect=extract):
            self.assertEqual(file_text(file),'')
        file.refresh_from_db()
        self.assertEqual(file.content,'')

    def test_deployment_invalidates_only_legacy_editor_extractions(self):
        saved = make_doc(self.user, 'legacy.docx', 'Stale legacy editor text')
        saved.status='Saved'
        saved.save(update_fields=['status'])
        other = make_doc(self.user, 'untouched.docx', 'Current extraction')
        import_module('core.migrations.0015_invalidate_saved_document_text').invalidate_saved_text(apps,None)
        saved.refresh_from_db();other.refresh_from_db()
        self.assertEqual(saved.content,'')
        self.assertEqual(other.content,'Current extraction')
