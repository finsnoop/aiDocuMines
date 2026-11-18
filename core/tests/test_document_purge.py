"""Synthetic all-tenant purge: real files/rows, isolated external-store adapters."""
import io
import json
import os
from pathlib import Path
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command, CommandError
from django.db import connection
from django.test import TestCase, override_settings

from core import document_purge as purge
from core.models import File, Run, Storage
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client, Document
from document_automation.models import AutomationTemplate, TemplateField
from document_operations.models import FileFolderLink, FileVersion, Folder
from document_workflows.models import Workflow, WorkflowRun, WorkflowStep
from home_dash_analytics.models import HomeAnalyticsSnapshot
from integrations.models import NextcloudConnection
from system_settings.models import SystemSettings


class DocumentPurgeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Purge firm")
        cls.foreign_firm = Client.objects.create(name="Other purge firm")
        cls.owner = make_user("purge-owner@example.test", cls.firm)
        cls.other = make_user("other-purge-owner@example.test", cls.foreign_firm)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.media = self.base / "media"
        self.media.mkdir()
        settings = override_settings(MEDIA_ROOT=str(self.media))
        settings.enable(); self.addCleanup(settings.disable)
        self.search = mock.patch("core.document_purge.search_inventory", return_value=({"elasticsearch": {"documents": 2}, "milvus": {"estimated_vectors": 2}}, []))
        self.search_adapter = self.search.start(); self.addCleanup(self.search.stop)
        self.keys = mock.patch("core.document_purge.document_cache_keys", return_value=set())
        self.keys.start(); self.addCleanup(self.keys.stop)
        self.file = self.document(self.owner, "source.txt")
        self.foreign_file = self.document(self.other, "foreign-source.txt")

    def document(self, owner, name):
        path = self.media / "uploads" / str(owner.pk) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Sensitive document " + name)
        run = Run.objects.create(user=owner, status="Completed")
        storage = Storage.objects.create(user=owner, upload_storage_location=str(path), output_storage_location=str(path))
        file = File.objects.create(user=owner, run=run, storage=storage, filepath=str(path), filename=name,
                                   project_id="matter", service_id="docs", content="Sensitive source text")
        folder = Folder.objects.create(user=owner, name="Documents", project_id="matter", service_id="docs")
        FileFolderLink.objects.create(file=file, folder=folder)
        return file

    def command(self, **options):
        output = io.StringIO()
        call_command("purge_documents", stdout=output, **options)
        return json.loads(output.getvalue().splitlines()[-1])

    def test_preview_is_read_only_and_execution_requires_all_tenant_acknowledgement(self):
        report = self.command()
        self.assertEqual(report["mode"], "preview")
        self.assertEqual(report["rows"]["core.File"], 2)
        self.assertTrue(File.objects.filter(pk=self.file.pk).exists())
        self.assertTrue(Path(self.file.filepath).exists())
        self.search_adapter.assert_called_once_with()
        with self.assertRaises(CommandError):
            self.command(execute=True)
        self.assertTrue(Path(self.file.filepath).exists())

    def test_execute_removes_all_tenant_documents_artifacts_and_orphans_but_keeps_config(self):
        template = AutomationTemplate.objects.create(name="Keep template", owner=self.owner, file=self.file,
            template_type="TXT", project_id="matter", service_id="automation", client_name=self.owner.username)
        field = TemplateField.objects.create(template=template, name="client", default_value="Keep field")
        workflow = Workflow.objects.create(name="Keep workflow", project_id="matter", service_id="workflow", client_name=self.owner.username)
        step = WorkflowStep.objects.create(workflow=workflow, step_number=1, name="Review", assignee_role="Client")
        WorkflowRun.objects.create(workflow=workflow, file=self.file, project_id="matter", service_id="workflow", client_name=self.owner.username)
        connector = NextcloudConnection.objects.create(user=self.owner)
        config, _ = SystemSettings.objects.get_or_create(client=self.firm)
        orphan = self.media / "uploads" / "orphan-private-mapping.json"; orphan.write_text("Private entity mapping")
        version = self.media / "versions" / "v1.txt"; version.parent.mkdir(); version.write_text("Old source")
        FileVersion.objects.create(file=self.file, version_number=1, file_path=str(version), uploaded_by=self.owner)
        avatar = self.media / "avatars" / "keep.png"; avatar.parent.mkdir(); avatar.write_bytes(b"Avatar")
        backups = self.base / "backups"; backups.mkdir(); backup = backups / "keep.tar.gz"; backup.write_bytes(b"Backup")
        Document.objects.create(title="Legacy document", content="Sensitive legacy body")
        HomeAnalyticsSnapshot.objects.create(user=self.owner, payload={"excerpt": "Sensitive source text"})
        report = self.command(execute=True, all_tenants=True)
        self.assertTrue(report["complete"])
        self.assertEqual(File.objects.count(), 0)
        self.assertEqual(Storage.objects.count(), 0)
        self.assertEqual(Run.objects.count(), 0)
        self.assertFalse(version.exists() or orphan.exists() or Path(self.file.filepath).exists() or Path(self.foreign_file.filepath).exists())
        template.refresh_from_db(); self.assertIsNone(template.file_id)
        self.assertTrue(TemplateField.objects.filter(pk=field.pk).exists())
        self.assertTrue(Workflow.objects.filter(pk=workflow.pk).exists() and WorkflowStep.objects.filter(pk=step.pk).exists())
        self.assertFalse(WorkflowRun.objects.exists() or Document.objects.exists() or HomeAnalyticsSnapshot.objects.exists())
        self.assertTrue(NextcloudConnection.objects.filter(pk=connector.pk).exists())
        self.assertTrue(SystemSettings.objects.filter(pk=config.pk).exists())
        self.assertEqual(get_user_model().objects.filter(pk__in=[self.owner.pk, self.other.pk]).count(), 2)
        self.assertTrue(avatar.exists() and backup.exists())
        self.assertEqual(self.search_adapter.call_args_list, [mock.call(), mock.call(execute=True)])
        self.assertTrue(self.command(execute=True, all_tenants=True)["complete"])

    def test_outside_root_path_blocks_before_any_deletion(self):
        outside = self.base / "outside.txt"; outside.write_text("Must remain")
        File.objects.filter(pk=self.file.pk).update(filepath=str(outside))
        with self.assertRaises(CommandError):
            self.command(execute=True, all_tenants=True)
        self.assertTrue(outside.exists() and Path(self.foreign_file.filepath).exists())
        self.assertEqual(File.objects.count(), 2)

    def test_explicit_additional_root_allows_only_its_recorded_path(self):
        root = self.base / "owned-document-store"; root.mkdir()
        path = root / "source.txt"; path.write_text("Explicit owned document")
        unreferenced = root / "not-swept.txt"; unreferenced.write_text("No implicit broad sweep")
        File.objects.filter(pk=self.file.pk).update(filepath=str(path))
        self.command(execute=True, all_tenants=True, storage_root=[str(root)])
        self.assertFalse(path.exists())
        self.assertTrue(unreferenced.exists())

    def test_nested_symlink_and_symlink_root_block_without_following_targets(self):
        target = self.base / "outside"; target.mkdir(); document = target / "keep.txt"; document.write_text("Must remain")
        link = self.media / "uploads" / "escape"; link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(CommandError):
            self.command(execute=True, all_tenants=True)
        self.assertTrue(document.exists() and Path(self.file.filepath).exists())
        with self.assertRaises(purge.PurgeBlocked):
            purge.owned_roots([str(link)])

    def test_safe_unlink_rejects_a_parent_replaced_after_inventory(self):
        original = Path(self.file.filepath)
        target = self.base / "outside"; target.mkdir(); foreign = target / original.name; foreign.write_text("Must remain")
        original.unlink(); original.parent.rmdir(); original.parent.symlink_to(target, target_is_directory=True)
        with self.assertRaises(purge.PurgeBlocked):
            purge.unlink_document(original, purge.owned_roots())
        self.assertTrue(foreign.exists())

    def test_broad_root_and_remote_recorded_path_are_rejected(self):
        with self.assertRaises(purge.PurgeBlocked):
            purge.owned_roots(["/"])
        with self.assertRaises(purge.PurgeBlocked):
            purge.checked_path("https://remote.example/document.pdf", purge.owned_roots())

    def test_unavailable_search_preflight_makes_no_document_changes(self):
        self.search_adapter.return_value = ({}, ["Milvus unavailable"])
        with self.assertRaises(CommandError):
            self.command(execute=True, all_tenants=True)
        self.assertEqual(File.objects.count(), 2)
        self.assertTrue(Path(self.file.filepath).exists())

    def test_search_failure_after_database_cleanup_reports_incomplete(self):
        self.search_adapter.side_effect = [({}, []), ({}, ["Elasticsearch cleanup incomplete"])]
        with self.assertRaises(CommandError):
            self.command(execute=True, all_tenants=True)
        self.assertFalse(File.objects.exists())
        self.assertFalse(Path(self.file.filepath).exists())

    def test_unlink_failure_retains_database_rows_for_retry(self):
        with mock.patch("core.document_purge.unlink_document", side_effect=PermissionError("Read-only storage")):
            with self.assertRaises(CommandError):
                self.command(execute=True, all_tenants=True)
        self.assertEqual(File.objects.count(), 2)
        self.assertTrue(Path(self.file.filepath).exists())

    def test_node_cache_allowlist_does_not_touch_users_or_settings_tables(self):
        tables = {"files": "id_user integer, id_node text, data_type text, metadata text, node text",
                  "ocr": "id_original_file integer, run_id text, ocr_option text, ocr_file text",
                  "anonymization": "id_original_file integer, run_id text, selectedFileType text, anonymization_file text",
                  "translation": "id_original_file integer, run_id text, to_lang text, translated_file text",
                  "activitylogs": "id_original_file integer, details text, activitylogs_file text"}
        with connection.cursor() as cursor:
            for name, columns in tables.items():
                cursor.execute(f'CREATE TEMP TABLE "{name}" ({columns})')
                cursor.execute(f'INSERT INTO "{name}" VALUES (' + ','.join(['%s'] * len(columns.split(','))) + ')', [1] * len(columns.split(',')))
            cursor.execute('INSERT INTO activitylogs VALUES (NULL, %s, %s)', ["Keep platform event", "{}"])
        try:
            found = purge.node_inventory("default")
            self.assertEqual(set(found), set(tables))
            purge.clear_node_tables("default", found)
            with connection.cursor() as cursor:
                for name in tables:
                    cursor.execute(f'SELECT COUNT(*) FROM "{name}"')
                    self.assertEqual(cursor.fetchone()[0], 1 if name == "activitylogs" else 0)
            self.assertEqual(get_user_model().objects.filter(pk__in=[self.owner.pk, self.other.pk]).count(), 2)
        finally:
            with connection.cursor() as cursor:
                for name in tables:
                    cursor.execute(f'DROP TABLE "{name}"')

    def test_document_cache_cleanup_does_not_clear_other_namespaces(self):
        cache.set("pair-assistant:purge-test", {"text": "Sensitive response"}, 60)
        cache.set("agents:resource-report-purge-test", {"state": "Keep operational config"}, 60)
        try:
            purge.clear_document_cache({"pair-assistant:purge-test"})
            self.assertIsNone(cache.get("pair-assistant:purge-test"))
            self.assertIsNotNone(cache.get("agents:resource-report-purge-test"))
        finally:
            cache.delete("agents:resource-report-purge-test")

    def test_core_run_backing_business_connector_configuration_is_preserved(self):
        from private_equity.models import DataRoomConnector, DueDiligenceRun
        business = DueDiligenceRun.objects.create(client=self.firm, run=self.file.run, deal_name="Keep deal", target_company="Keep company")
        connector = DataRoomConnector.objects.create(client=self.firm, user=self.owner, due_diligence_run=business,
            connector_name="Keep connector", connector_type="sharepoint")
        report = self.command(execute=True, all_tenants=True)
        self.assertGreaterEqual(report["preserved_business_runs"], 1)
        self.assertTrue(DataRoomConnector.objects.filter(pk=connector.pk).exists())
        self.assertTrue(DueDiligenceRun.objects.filter(pk=business.pk).exists())
        self.assertTrue(Run.objects.filter(pk=business.run_id).exists())
        self.assertFalse(File.objects.exists())
