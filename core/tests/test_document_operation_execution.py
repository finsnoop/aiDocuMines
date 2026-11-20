"""Exercise context-menu endpoints, real worker functions and stored output bytes."""
import contextlib
import io
from pathlib import Path
import tempfile
import uuid
import zipfile
from unittest import mock

from celery import current_app
from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import File, Run
from core.tests.test_tenancy import make_user, token_for
from custom_authentication.models import Client
from document_operations.models import EffectiveAccess, FileAccessEntry, FileAlias, FileFolderLink, FileVersion, Folder
from document_operations.tasks import async_duplicate_file, async_password_protect_file, async_rename_file, async_zip_files


class DocumentOperationExecutionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Operation firm")
        cls.other_firm = Client.objects.create(name="Other operation firm")
        cls.owner = make_user("owner@operations.test", cls.firm, groups=["Client"])
        cls.reader = make_user("reader@operations.test", cls.firm, groups=["Client"])
        cls.admin = make_user("admin@operations.test", cls.firm, staff=True, groups=["Admin"])
        cls.foreign = make_user("foreign@operations.test", cls.other_firm, groups=["Client"])
        cls.guest = make_user("guest@operations.test", cls.firm, groups=["Guest"])
        cls.tokens = {u.pk: token_for(u)[1] for u in [cls.owner, cls.reader, cls.admin, cls.foreign, cls.guest]}

    def setUp(self):
        cache.clear()
        self.context = contextlib.ExitStack()
        self.addCleanup(self.context.close)
        self.media = Path(self.context.enter_context(tempfile.TemporaryDirectory()))
        self.context.enter_context(override_settings(MEDIA_ROOT=str(self.media), CELERY_TASK_ALWAYS_EAGER=True,
                                                      CELERY_TASK_EAGER_PROPAGATES=False, CELERY_TASK_STORE_EAGER_RESULT=True))
        self.saved_conf = {name: current_app.conf.get(name) for name in ["task_always_eager", "task_eager_propagates", "task_store_eager_result"]}
        current_app.conf.update(task_always_eager=True, task_eager_propagates=False, task_store_eager_result=True)
        self.addCleanup(lambda: current_app.conf.update(**self.saved_conf))
        for name, task in current_app.tasks.items():
            if name.startswith("document_operations.tasks."):
                self.context.enter_context(mock.patch.object(task, "store_eager_result", True))
                self.context.enter_context(mock.patch.object(task, "ignore_result", False))
        self.folder = self.make_folder(self.owner, "Originals", "Matter A")
        self.file, self.link = self.make_document(self.owner, self.folder, "contract.txt", b"Original contract bytes.", link_id=99001)

    def api(self, user=None):
        token = self.tokens[(user or self.owner).pk]
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer " + token.token, HTTP_X_CLIENT_ID=token.application.client_id)
        return client

    def make_folder(self, owner, name, project="Matter A"):
        return Folder.objects.create(user=owner, name=name, project_id=project, service_id="documents")

    def make_document(self, owner, folder, name, content, link_id=None):
        directory = self.media / uuid.uuid4().hex
        directory.mkdir()
        path = directory / name
        path.write_bytes(content)
        run = Run.objects.create(user=owner, status="Completed")
        document = File.objects.create(user=owner, run=run, filename=name, filepath=str(path), file_size=len(content),
                                       file_type="text/plain", extension=Path(name).suffix.lstrip("."),
                                       project_id=folder.project_id, service_id=folder.service_id, status="Completed",
                                       content=content.decode("utf-8", errors="replace"))
        link = FileFolderLink.objects.create(file=document, folder=folder, **({"id": link_id} if link_id else {}))
        return document, link

    def bytes(self, response):
        self.assertEqual(response.status_code, 200, getattr(response, "content", b""))
        value = b"".join(response.streaming_content) if response.streaming else response.content
        # Django's test-client streaming wrapper closes the response safely at
        # exhaustion; closing it twice would emit request_finished twice.
        return value

    def complete(self, response, user=None):
        self.assertEqual(response.status_code, 202, response.content)
        self.assertIn("task_id", response.json())
        status = self.api(user).get("/api/v1/documents/tasks/" + response.json()["task_id"] + "/")
        self.assertEqual(status.status_code, 200, status.content)
        self.assertEqual(status.json()["status"], "success", status.content)
        return status.json()["result"]

    def test_context_info_preview_and_download_use_the_correct_id_contracts(self):
        response = self.api().get(f"/api/v1/documents/table/{self.file.pk}/context/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["node"]["key"], str(self.link.pk))
        self.assertEqual(response.json()["node"]["data"]["file_id"], self.file.pk)
        info = self.api().get(f"/api/v1/documents/files/{self.link.pk}/")
        self.assertEqual(info.status_code, 200, info.content)
        self.assertEqual(info.json()["id"], self.file.pk)
        self.assertEqual(self.bytes(self.api().get(f"/api/v1/documents/files/{self.link.pk}/preview/")), b"Original contract bytes.")
        self.assertEqual(self.bytes(self.api().get(f"/api/v1/core/download/?file_id={self.file.pk}")), b"Original contract bytes.")

    def test_rename_executes_and_cannot_overwrite_an_existing_document(self):
        collision = Path(self.file.filepath).with_name("collision.txt")
        collision.write_bytes(b"Other document bytes.")
        result = self.complete(self.api().patch(f"/api/v1/documents/files/{self.link.pk}/rename/", {"new_name": "collision.txt"}, format="json"))
        self.file.refresh_from_db()
        self.assertEqual(result["file_id"], self.file.pk)
        self.assertEqual(self.file.filename, "collision.txt")
        self.assertEqual(Path(self.file.filepath).read_bytes(), b"Original contract bytes.")
        self.assertEqual(collision.read_bytes(), b"Other document bytes.")

    def test_copy_has_private_bytes_requester_ownership_and_destination_metadata(self):
        FileAccessEntry.objects.create(file_link=self.link, user=self.reader, can_read=True)
        target = self.make_folder(self.reader, "My copied documents", "Matter B")
        result = self.complete(self.api(self.reader).post(f"/api/v1/documents/files/{self.link.pk}/copy/", {"target_folder": str(target.pk)}, format="json"), self.reader)
        copied = File.objects.get(pk=result["file_id"])
        self.assertNotEqual(copied.filepath, self.file.filepath)
        self.assertEqual(copied.user_id, self.reader.pk)
        self.assertEqual(copied.project_id, "Matter B")
        self.assertEqual(copied.origin_file_id, self.file.pk)
        self.assertIsNotNone(copied.storage_id)
        self.assertEqual(copied.folder_links.get().folder_id, target.pk)
        self.assertTrue(copied.filename.endswith(".txt"))
        self.assertEqual(Path(copied.filepath).read_bytes(), b"Original contract bytes.")
        self.assertEqual(self.bytes(self.api(self.reader).get(result["download_url"])), b"Original contract bytes.")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.api(self.reader).delete(f"/api/v1/documents/files/{result['file_link_id']}/delete/").status_code, 200)
        self.assertFalse(Path(copied.filepath).exists())
        self.assertTrue(Path(self.file.filepath).exists())

    def test_duplicate_is_a_separate_registered_document(self):
        result = self.complete(self.api().post(f"/api/v1/documents/files/{self.link.pk}/duplicate/", {}, format="json"))
        copied = File.objects.get(pk=result["file_id"])
        self.assertNotEqual(copied.pk, self.file.pk)
        self.assertNotEqual(copied.filepath, self.file.filepath)
        self.assertEqual(copied.folder_links.get().folder_id, self.folder.pk)
        self.assertEqual(copied.content, self.file.content)
        self.assertEqual(copied.status, "Completed")

    def test_move_updates_folder_and_matter_metadata_and_checks_target_access(self):
        target = self.make_folder(self.owner, "Target", "Matter B")
        self.complete(self.api().post("/api/v1/documents/files/move/", {"file_ids": [self.link.pk], "target_folder": str(target.pk)}, format="json"))
        self.link.refresh_from_db(); self.file.refresh_from_db()
        self.assertEqual(self.link.folder_id, target.pk)
        self.assertEqual(self.file.project_id, "Matter B")
        foreign_target = self.make_folder(self.foreign, "Forbidden", "Other matter")
        response = self.api().post("/api/v1/documents/files/move/", {"file_ids": [self.link.pk], "target_folder": str(foreign_target.pk)}, format="json")
        self.assertEqual(response.status_code, 404)

    def test_archive_restore_and_delete_preserve_another_link_to_the_same_file(self):
        self.complete(self.api().post("/api/v1/documents/files/trash/", {"file_ids": [self.link.pk]}, format="json"))
        self.link.refresh_from_db(); self.assertTrue(self.link.is_trashed)
        self.assertEqual(self.api().post(f"/api/v1/documents/files/{self.link.pk}/restore/", {}, format="json").status_code, 200)
        self.link.refresh_from_db(); self.assertFalse(self.link.is_trashed)
        other = FileFolderLink.objects.create(file=self.file, folder=self.make_folder(self.owner, "Other location"))
        self.assertEqual(self.api().delete(f"/api/v1/documents/files/{self.link.pk}/delete/").status_code, 200)
        self.assertTrue(File.objects.filter(pk=self.file.pk).exists())
        self.assertTrue(FileFolderLink.objects.filter(pk=other.pk).exists())
        self.assertTrue(Path(self.file.filepath).exists())

    def test_zip_persists_a_downloadable_archive_and_preserves_duplicate_names(self):
        second, second_link = self.make_document(self.owner, self.folder, "contract.txt", b"Second contract bytes.")
        third, third_link = self.make_document(self.owner, self.folder, f"contract_{second_link.pk}.txt", b"Colliding generated name.")
        result = self.complete(self.api().post("/api/v1/documents/files/zip/", {"file_ids": [third_link.pk, self.link.pk, second_link.pk]}, format="json"))
        output = File.objects.get(pk=result["file_id"])
        self.assertEqual(output.user_id, self.owner.pk)
        self.assertIsNotNone(output.storage_id)
        self.assertEqual(output.folder_links.count(), 1)
        data = self.bytes(self.api().get(result["download_url"]))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(len(set(archive.namelist())), 3)
            self.assertEqual({archive.read(name) for name in archive.namelist()}, {b"Original contract bytes.", b"Second contract bytes.", b"Colliding generated name."})

    def test_read_share_downgrade_really_removes_write_delete_and_share_powers(self):
        url = f"/api/v1/documents/files/{self.link.pk}/share/"
        self.assertEqual(self.api().post(url, {"user_ids": [self.reader.pk], "access_level": "owner"}, format="json").status_code, 200)
        self.assertEqual(self.api().post(url, {"user_ids": [self.reader.pk], "access_level": "read"}, format="json").status_code, 200)
        grant = FileAccessEntry.objects.get(file_link=self.link, user=self.reader)
        self.assertTrue(grant.can_read)
        self.assertFalse(grant.can_write or grant.can_delete or grant.can_share)
        self.assertEqual(self.bytes(self.api(self.reader).get(f"/api/v1/documents/files/{self.link.pk}/preview/")), b"Original contract bytes.")
        for method, suffix, body in [("patch", "rename/", {"new_name": "stolen.txt"}), ("post", "color/", {"color": "red"}), ("post", "aliases/", {"alias_name": "changed"}), ("post", "share/public/", {})]:
            response = getattr(self.api(self.reader), method)(f"/api/v1/documents/files/{self.link.pk}/{suffix}", body, format="json")
            self.assertEqual(response.status_code, 404, response.content)

    def test_cross_tenant_share_and_copy_are_denied_without_output(self):
        response = self.api().post(f"/api/v1/documents/files/{self.link.pk}/share/", {"user_ids": [self.foreign.pk]}, format="json")
        self.assertEqual(response.status_code, 404)
        target = self.make_folder(self.foreign, "Foreign output")
        response = self.api(self.foreign).post(f"/api/v1/documents/files/{self.link.pk}/copy/", {"target_folder": str(target.pk)}, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(File.objects.count(), 1)

    def test_public_link_and_password_are_usable_and_revocable(self):
        response = self.api().post(f"/api/v1/documents/files/{self.link.pk}/share/public/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        token = response.json()["public_token"]
        self.assertGreaterEqual(len(token), 32)
        url = f"/api/v1/documents/public/share/{token}/"
        anonymous = APIClient()
        metadata = anonymous.get(url)
        self.assertEqual(metadata.status_code, 200, metadata.content)
        self.assertEqual(metadata.json()["filename"], self.file.filename)
        self.assertNotIn(self.file.filepath, metadata.content.decode())
        protected = self.api().post(f"/api/v1/documents/files/{self.link.pk}/protect/", {"password": "A real secret 2026", "password_hint": "Owner supplied hint"}, format="json")
        self.assertEqual(protected.status_code, 200, protected.content)
        self.link.refresh_from_db()
        self.assertNotEqual(self.link.password_hash, "A real secret 2026")
        self.assertTrue(check_password("A real secret 2026", self.link.password_hash))
        self.assertEqual(anonymous.get(url).status_code, 401)
        self.assertEqual(anonymous.get(url+"content/", HTTP_X_SHARE_PASSWORD="wrong").status_code, 401)
        self.assertEqual(self.bytes(anonymous.get(url+"content/", HTTP_X_SHARE_PASSWORD="A real secret 2026")), b"Original contract bytes.")
        self.assertEqual(self.api().post(f"/api/v1/documents/files/{self.link.pk}/unshare/", {}, format="json").status_code, 200)
        self.assertEqual(anonymous.get(url, HTTP_X_SHARE_PASSWORD="A real secret 2026").status_code, 404)

    def test_legacy_hint_only_worker_cannot_claim_or_set_password_protection(self):
        result = async_password_protect_file.apply(args=(self.link.pk, "Hint without password"),
            kwargs={"actor_user_id": self.owner.pk}, throw=False)
        self.assertIn("error", result.result)
        self.link.refresh_from_db()
        self.assertFalse(self.link.password_protected)
        self.assertEqual(self.link.password_hash, "")
        self.assertFalse(self.file.audit_logs.filter(action="password_set").exists())

    def test_unshare_revokes_every_explicit_file_grant_and_public_token(self):
        EffectiveAccess.objects.create(user=self.reader, file=self.file, can_download=True, can_duplicate=True)
        FileAccessEntry.objects.create(file_link=self.link, user=self.reader, can_read=True, can_write=True)
        self.link.shared_with.add(self.reader)
        self.link.is_public = True; self.link.public_token = "explicit-unshare-token"; self.link.save()
        self.assertEqual(self.api(self.reader).get(f"/api/v1/documents/files/{self.link.pk}/").status_code, 200)
        response = self.api().post(f"/api/v1/documents/files/{self.link.pk}/unshare/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(EffectiveAccess.objects.filter(file=self.file).exists())
        self.assertFalse(FileAccessEntry.objects.filter(file_link=self.link).exists())
        self.assertFalse(self.link.shared_with.exists())
        self.assertEqual(self.api(self.reader).get(f"/api/v1/documents/files/{self.link.pk}/").status_code, 404)
        self.assertEqual(APIClient().get("/api/v1/documents/public/share/explicit-unshare-token/").status_code, 404)

    def test_hint_only_legacy_protection_fails_closed(self):
        self.link.is_public = True; self.link.public_token = "legacy-token"; self.link.password_protected = True
        self.link.save()
        self.assertEqual(APIClient().get("/api/v1/documents/public/share/legacy-token/content/").status_code, 401)

    def test_version_restore_copies_real_bytes_and_keeps_the_previous_version(self):
        previous = self.media / "previous-version.txt"
        previous.write_bytes(b"Previous contract bytes.")
        FileVersion.objects.create(file=self.file, version_number=1, file_path=str(previous), uploaded_by=self.owner)
        response = self.api().post(f"/api/v1/documents/files/{self.link.pk}/versions/1/restore/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.file.refresh_from_db()
        self.assertEqual(Path(self.file.filepath).read_bytes(), b"Previous contract bytes.")
        self.assertIsNone(self.file.content)
        backup = self.file.versions.get(version_number=response.json()["backup_version"])
        self.assertEqual(Path(backup.file_path).read_bytes(), b"Original contract bytes.")

    def test_task_results_are_private_and_worker_rechecks_revoked_access(self):
        queued = self.api().post(f"/api/v1/documents/files/{self.link.pk}/duplicate/", {}, format="json")
        self.assertEqual(queued.status_code, 202, queued.content)
        task_id = queued.json()["task_id"]
        self.assertEqual(self.api(self.reader).get(f"/api/v1/documents/tasks/{task_id}/").status_code, 404)
        self.assertEqual(self.api(self.foreign).get(f"/api/v1/documents/tasks/{task_id}/").status_code, 404)
        FileAccessEntry.objects.create(file_link=self.link, user=self.reader, can_read=True, can_write=True)
        FileAccessEntry.objects.filter(user=self.reader).delete()
        result = async_rename_file.apply(args=(self.file.pk, "should-not-change.txt"), kwargs={"actor_user_id": self.reader.pk}, throw=False)
        self.assertTrue(result.failed())
        self.file.refresh_from_db(); self.assertEqual(self.file.filename, "contract.txt")

    def test_task_polling_rechecks_access_after_a_share_is_revoked(self):
        grant = FileAccessEntry.objects.create(file_link=self.link, user=self.reader, can_read=True, can_write=True)
        response = self.api(self.reader).patch(f"/api/v1/documents/files/{self.link.pk}/rename/", {"new_name": "reviewed.txt"}, format="json")
        self.complete(response, self.reader)
        grant.delete()
        task_id = response.json()["task_id"]
        self.assertEqual(self.api(self.reader).get(f"/api/v1/documents/tasks/{task_id}/").status_code, 404)

    def test_legacy_cross_tenant_grants_cannot_bypass_tenant_boundaries(self):
        FileAccessEntry.objects.create(file_link=self.link, user=self.foreign, can_read=True, can_write=True, can_share=True)
        EffectiveAccess.objects.create(user=self.foreign, file=self.file, can_download=True, can_rename=True, can_share=True)
        self.link.shared_with.add(self.foreign)
        for path in [f"/api/v1/documents/files/{self.link.pk}/", f"/api/v1/documents/files/{self.link.pk}/preview/",
                     f"/api/v1/documents/table/{self.file.pk}/context/", f"/api/v1/core/download/?file_id={self.file.pk}"]:
            self.assertEqual(self.api(self.foreign).get(path).status_code, 404, path)
        self.assertEqual(self.api(self.foreign).patch(f"/api/v1/documents/files/{self.link.pk}/rename/", {"new_name": "stolen.txt"}, format="json").status_code, 404)

    def test_cross_owner_move_requires_sharing_authority_and_does_not_create_grants(self):
        target = self.make_folder(self.reader, "My destination", "Other matter")
        FileAccessEntry.objects.create(file_link=self.link, user=self.reader, can_read=True, can_write=True)
        response = self.api(self.reader).post("/api/v1/documents/files/move/", {"file_ids": [self.link.pk], "target_folder": str(target.pk)}, format="json")
        self.assertEqual(response.status_code, 202, response.content)
        result = self.api(self.reader).get(f"/api/v1/documents/tasks/{response.json()['task_id']}/")
        self.assertEqual(result.json()["status"], "failure")
        self.link.refresh_from_db(); self.assertEqual(self.link.folder_id, self.folder.pk)
        self.assertEqual(FileAccessEntry.objects.filter(file_link=self.link).count(), 1)

    def test_queued_rename_refuses_a_document_edited_after_submission(self):
        original_version = self.file.updated_at.isoformat()
        File.objects.filter(pk=self.file.pk).update(updated_at=timezone.now())
        result = async_rename_file.apply(args=(self.file.pk, "stale.txt"), kwargs={"actor_user_id": self.owner.pk,
                                         "expected_updated_at": original_version}, throw=False)
        self.assertTrue(result.failed())
        self.file.refresh_from_db(); self.assertEqual(self.file.filename, "contract.txt")

    def test_failed_archive_reports_failure_and_does_not_register_missing_output(self):
        Path(self.file.filepath).unlink()
        response = self.api().post("/api/v1/documents/files/zip/", {"file_ids": [self.link.pk]}, format="json")
        self.assertEqual(response.status_code, 202, response.content)
        result = self.api().get(f"/api/v1/documents/tasks/{response.json()['task_id']}/")
        self.assertEqual(result.json()["status"], "failure")
        self.assertNotIn(str(self.media), result.content.decode())
        self.assertEqual(File.objects.count(), 1)

    def test_tenant_admin_can_run_a_real_operation_without_impersonating_the_owner(self):
        self.complete(self.api(self.admin).patch(f"/api/v1/documents/files/{self.link.pk}/rename/", {"new_name": "admin-reviewed.txt"}, format="json"), self.admin)
        self.file.refresh_from_db()
        self.assertEqual(self.file.filename, "admin-reviewed.txt")
        self.assertEqual(self.file.audit_logs.filter(action="renamed").get().user_id, self.admin.pk)

    def test_deleting_a_folder_keeps_documents_linked_from_other_folders(self):
        other = FileFolderLink.objects.create(file=self.file, folder=self.make_folder(self.owner, "Another location"))
        response = self.api().delete(f"/api/v1/documents/folders/{self.folder.pk}/delete/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(File.objects.filter(pk=self.file.pk).exists())
        self.assertTrue(FileFolderLink.objects.filter(pk=other.pk).exists())
        self.assertTrue(Path(self.file.filepath).exists())

    def test_group_share_is_limited_to_colleagues_in_the_same_tenant(self):
        group = Group.objects.create(name="Cross-tenant test team")
        self.reader.groups.add(group); self.foreign.groups.add(group)
        response = self.api().post(f"/api/v1/documents/files/{self.link.pk}/share/group/", {"group_name": group.name, "access_level": "read"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["shared_with"], [self.reader.pk])
        self.assertFalse(FileAccessEntry.objects.filter(user=self.foreign).exists())
        self.assertEqual(self.api(self.foreign).get(f"/api/v1/documents/files/{self.link.pk}/preview/").status_code, 404)

    def test_unaffiliated_user_cannot_use_legacy_shared_grants(self):
        personal = make_user("personal@operations.test", None, groups=["Client"])
        self.tokens[personal.pk] = token_for(personal)[1]
        FileAccessEntry.objects.create(file_link=self.link, user=personal, can_read=True, can_write=True)
        EffectiveAccess.objects.create(user=personal, file=self.file, can_download=True, can_protect=True)
        self.link.shared_with.add(personal)
        self.assertEqual(self.api(personal).get(f"/api/v1/core/download/?file_id={self.file.pk}").status_code, 404)
        self.assertEqual(self.api(personal).post(f"/api/v1/documents/files/{self.link.pk}/protect/", {"password": "A real password 2026"}, format="json").status_code, 404)

    def test_file_access_entry_retains_its_documented_read_write_fallback(self):
        grant = FileAccessEntry.objects.create(file_link=self.link, user=self.reader, can_read=True)
        self.complete(self.api(self.reader).post("/api/v1/documents/files/zip/", {"file_ids": [self.link.pk]}, format="json"), self.reader)
        self.assertEqual(self.api(self.reader).post(f"/api/v1/documents/files/{self.link.pk}/protect/", {"password": "A real password 2026"}, format="json").status_code, 404)
        grant.can_write = True; grant.save(update_fields=["can_write"])
        self.assertEqual(self.api(self.reader).post(f"/api/v1/documents/files/{self.link.pk}/protect/", {"password": "A real password 2026"}, format="json").status_code, 200)
        self.link.is_trashed = True; self.link.save(update_fields=["is_trashed"])
        self.assertEqual(self.api(self.reader).post(f"/api/v1/documents/files/{self.link.pk}/restore/", {}, format="json").status_code, 200)

    def test_download_only_grant_denies_dedicated_mutations_and_zip_before_queue(self):
        EffectiveAccess.objects.create(user=self.reader, file=self.file, can_download=True, can_rename=True)
        urls = [(f"files/{self.link.pk}/duplicate/", {}),
                (f"files/{self.link.pk}/protect/", {"password": "A real password 2026"}),
                (f"files/{self.link.pk}/restore/", {}),
                (f"files/{self.link.pk}/versions/1/restore/", {}),
                ("files/zip/", {"file_ids": [self.link.pk]})]
        for path, body in urls:
            response = self.api(self.reader).post("/api/v1/documents/" + path, body, format="json")
            self.assertEqual(response.status_code, 404, response.content)
            self.assertNotIn("task_id", response.json())
        self.assertEqual(File.objects.count(), 1)
        self.link.refresh_from_db(); self.assertFalse(self.link.password_protected)

    def test_duplicate_grant_without_download_executes_and_polls_with_its_own_permission(self):
        destination = self.make_folder(self.reader, "Shared location")
        self.link.folder = destination; self.link.save(update_fields=["folder"])
        EffectiveAccess.objects.create(user=self.reader, file=self.file, can_duplicate=True)
        result = self.complete(self.api(self.reader).post(f"/api/v1/documents/files/{self.link.pk}/duplicate/", {}, format="json"), self.reader)
        output = File.objects.get(pk=result["file_id"])
        self.assertEqual(output.user_id, self.reader.pk)
        self.assertEqual(output.folder_links.get().folder_id, destination.pk)
        self.assertEqual(self.bytes(self.api(self.reader).get(result["download_url"])), b"Original contract bytes.")

    def test_zip_grant_without_download_executes_and_polls_with_its_own_permission(self):
        grant = EffectiveAccess.objects.create(user=self.reader, file=self.file, can_zip=True)
        result = self.complete(self.api(self.reader).post("/api/v1/documents/files/zip/", {"file_ids": [self.link.pk]}, format="json"), self.reader)
        with zipfile.ZipFile(io.BytesIO(self.bytes(self.api(self.reader).get(result["download_url"])))) as archive:
            self.assertEqual(archive.read("contract.txt"), b"Original contract bytes.")
        count = File.objects.count()
        grant.can_zip = False; grant.save(update_fields=["can_zip"])
        queued = async_zip_files.apply(args=([self.link.pk],), kwargs={"actor_user_id": self.reader.pk}, throw=False)
        self.assertTrue(queued.failed())
        self.assertEqual(File.objects.count(), count)

    def test_protect_grant_does_not_require_rename_and_hashes_the_password(self):
        EffectiveAccess.objects.create(user=self.reader, file=self.file, can_protect=True)
        response = self.api(self.reader).post(f"/api/v1/documents/files/{self.link.pk}/protect/", {"password": "A real password 2026"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.link.refresh_from_db(); self.assertTrue(check_password("A real password 2026", self.link.password_hash))
        self.assertEqual(self.api(self.reader).patch(f"/api/v1/documents/files/{self.link.pk}/rename/", {"new_name": "denied.txt"}, format="json").status_code, 404)

    def test_restore_grant_restores_archive_and_version_without_rename_or_delete(self):
        EffectiveAccess.objects.create(user=self.reader, file=self.file, can_restore=True)
        self.link.is_trashed = True; self.link.save(update_fields=["is_trashed"])
        self.assertEqual(self.api(self.reader).post(f"/api/v1/documents/files/{self.link.pk}/restore/", {}, format="json").status_code, 200)
        self.link.refresh_from_db(); self.assertFalse(self.link.is_trashed)
        previous = self.media / "grant-version.txt"; previous.write_bytes(b"Grant-restored bytes.")
        FileVersion.objects.create(file=self.file, version_number=1, file_path=str(previous), uploaded_by=self.owner)
        response = self.api(self.reader).post(f"/api/v1/documents/files/{self.link.pk}/versions/1/restore/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.file.refresh_from_db(); self.assertEqual(Path(self.file.filepath).read_bytes(), b"Grant-restored bytes.")

    def test_context_dedicated_permissions_match_operation_and_folder_authority(self):
        EffectiveAccess.objects.create(user=self.reader, file=self.file, can_download=True, can_duplicate=True, can_zip=True,
                                       can_protect=True, can_restore=True, can_move=True)
        EffectiveAccess.objects.create(user=self.reader, folder=self.folder, can_download=True)
        response = self.api(self.reader).get(f"/api/v1/documents/table/{self.file.pk}/context/")
        self.assertEqual(response.status_code, 200, response.content)
        permissions = response.json()["permissions"]
        self.assertTrue(permissions["download"] and permissions["copy"] and permissions["versions"])
        self.assertTrue(permissions["zip"] and permissions["protect"] and permissions["restore"])
        self.assertFalse(permissions["duplicate"] or permissions["move"] or permissions["rename"])

    def test_worker_reloads_role_after_a_queued_duplicate_was_requested(self):
        folder = self.make_folder(self.reader, "Reader destination")
        self.link.folder = folder; self.link.save(update_fields=["folder"])
        EffectiveAccess.objects.create(user=self.reader, file=self.file, can_duplicate=True)
        self.reader.groups.clear(); self.reader.groups.add(Group.objects.get(name="Guest"))
        result = async_duplicate_file.apply(args=(self.link.pk,), kwargs={"actor_user_id": self.reader.pk}, throw=False)
        self.assertTrue(result.failed())
        self.assertEqual(File.objects.count(), 1)

    def test_colour_and_alias_round_trip_and_guest_write_rejection(self):
        url = f"/api/v1/documents/files/{self.link.pk}/"
        self.assertEqual(self.api().post(url+"color/", {"color": "green"}, format="json").status_code, 201)
        alias = self.api().post(url+"aliases/", {"alias_name": "Signed contract"}, format="json")
        self.assertEqual(alias.status_code, 201, alias.content)
        self.assertEqual(self.api().get(url+"aliases/").json()[0]["alias_name"], "Signed contract")
        self.assertEqual(self.api().delete(url+f"aliases/{alias.json()['id']}/").status_code, 200)
        self.assertFalse(FileAlias.objects.exists())
        self.assertEqual(self.api().delete(url+"color/").status_code, 200)
        guest_folder = self.make_folder(self.guest, "Guest originals")
        guest_file, guest_link = self.make_document(self.guest, guest_folder, "guest.txt", b"Guest readable document.")
        self.assertEqual(self.bytes(self.api(self.guest).get(f"/api/v1/documents/files/{guest_link.pk}/preview/")), b"Guest readable document.")
        self.assertEqual(self.bytes(self.api(self.guest).get(f"/api/v1/core/download/?file_id={guest_file.pk}")), b"Guest readable document.")
        self.assertEqual(self.api(self.guest).post(f"/api/v1/documents/files/{guest_link.pk}/color/", {"color": "red"}, format="json").status_code, 403)
