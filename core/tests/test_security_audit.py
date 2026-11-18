"""
Regression tests for the November 2026 security and reliability audit.

Each test states one rule that was broken before the audit. Two organisations
(Firm A, Firm B); a1 and a2 are colleagues in Firm A, b1 works at Firm B.
Run: python manage.py test core.tests.test_security_audit
"""
import os
from datetime import timedelta
from unittest import mock
from types import SimpleNamespace

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application
from rest_framework.test import APIClient

from core import access, maintenance
from core.models import File, Metadata
from core.tests.test_tenancy import User, make_file, make_user, token_for
from custom_authentication.models import Client, PasswordResetToken
from document_operations.models import FileFolderLink, Folder

NO_SEARCH = dict(ELASTICSEARCH_DSL_AUTOSYNC=False, ELASTICSEARCH_DSL_AUTO_REFRESH=False)
# Throttling is covered by its own test; everywhere else it must not interfere.
OPEN_RATES = {"auth": "10000/min", "auth_network": "10000/min", "anon": "10000/min", "user": "10000/min"}


def api(tok=None, **headers):
    c = APIClient()
    if tok is not None:
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {tok.token}", **headers)
    return c


def linked_file(owner, name="doc.pdf", folder_name="Contracts"):
    f = make_file(owner, name)
    folder = Folder.objects.create(name=folder_name, user=owner, project_id="p1", service_id="s1")
    link = FileFolderLink.objects.create(file=f, folder=folder)
    return f, folder, link


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm_a = Client.objects.create(name="Firm A")
        cls.firm_b = Client.objects.create(name="Firm B")
        cls.a1 = make_user("a1@firm-a.test", cls.firm_a, groups=["Client"])
        cls.a2 = make_user("a2@firm-a.test", cls.firm_a, groups=["Client"])
        cls.a_admin = make_user("admin@firm-a.test", cls.firm_a, staff=True, groups=["Admin"])
        cls.b1 = make_user("b1@firm-b.test", cls.firm_b, groups=["Client"])
        cls.b_admin = make_user("b-admin@firm-b.test", cls.firm_b, staff=True, groups=["Admin"])
        cls.app_a1, cls.tok_a1 = token_for(cls.a1)
        cls.app_a2, cls.tok_a2 = token_for(cls.a2)
        cls.app_admin, cls.tok_admin = token_for(cls.a_admin)
        cls.app_b1, cls.tok_b1 = token_for(cls.b1)
        cls.app_badmin, cls.tok_badmin = token_for(cls.b_admin)
        cls.file_a1, cls.folder_a1, cls.link_a1 = linked_file(cls.a1, "a1-secret.pdf")

    def setUp(self):
        cache.clear()

    def outsiders(self):
        """A colleague without a share, a stranger from another firm, and that firm's admin."""
        return [("colleague", self.tok_a2), ("other organisation", self.tok_b1),
                ("other organisation admin", self.tok_badmin)]


@override_settings(**NO_SEARCH)
class DocumentOperationTests(Base):
    """Nobody may act on a file or folder by guessing its id."""

    def assert_untouched(self):
        self.link_a1.refresh_from_db()
        self.folder_a1.refresh_from_db()
        self.file_a1.refresh_from_db()
        self.assertFalse(self.link_a1.is_trashed)
        self.assertFalse(self.link_a1.password_protected)
        self.assertEqual(self.link_a1.folder_id, self.folder_a1.id)
        self.assertEqual(self.file_a1.filename, "a1-secret.pdf")
        self.assertEqual(self.folder_a1.name, "Contracts")
        self.assertFalse(self.folder_a1.is_trashed)

    @mock.patch("document_operations.views.async_bulk_trash_files.delay")
    def test_bulk_trash_requires_access_to_every_file(self, delay):
        delay.return_value = SimpleNamespace(id="security-trash-task")
        for who, tok in self.outsiders():
            r = api(tok).post("/api/v1/documents/files/trash/", {"file_ids": [self.link_a1.id]}, format="json")
            self.assertEqual(r.status_code, 404, who)
        delay.assert_not_called()
        r = api(self.tok_a1).post("/api/v1/documents/files/trash/", {"file_ids": [self.link_a1.id]}, format="json")
        self.assertEqual(r.status_code, 202)
        delay.assert_called_once_with([self.link_a1.id], actor_user_id=self.a1.pk)

    @mock.patch("document_operations.views.async_bulk_trash_files.delay")
    def test_one_foreign_id_fails_the_whole_batch(self, delay):
        _, _, mine = linked_file(self.a2, "a2.pdf", "Mine")
        r = api(self.tok_a2).post("/api/v1/documents/files/trash/",
                                  {"file_ids": [mine.id, self.link_a1.id]}, format="json")
        self.assertEqual(r.status_code, 404)
        delay.assert_not_called()

    @mock.patch("document_operations.views.async_bulk_move_files_to_folder.delay")
    def test_move_checks_files_and_target_folder(self, delay):
        own_folder = Folder.objects.create(name="Mine", user=self.b1, project_id="p", service_id="s")
        r = api(self.tok_b1).post("/api/v1/documents/files/move/",
                                  {"file_ids": [self.link_a1.id], "target_folder": str(own_folder.id)}, format="json")
        self.assertEqual(r.status_code, 404)
        # Own file into somebody else's folder is refused too.
        _, _, b_link = linked_file(self.b1, "b.pdf", "B")
        r = api(self.tok_b1).post("/api/v1/documents/files/move/",
                                  {"file_ids": [b_link.id], "target_folder": str(self.folder_a1.id)}, format="json")
        self.assertEqual(r.status_code, 404)
        delay.assert_not_called()

    @mock.patch("document_operations.views.async_rename_file.delay")
    def test_rename_file(self, delay):
        delay.return_value = SimpleNamespace(id="security-rename-task")
        for who, tok in self.outsiders():
            r = api(tok).patch(f"/api/v1/documents/files/{self.link_a1.id}/rename/", {"new_name": "x.pdf"}, format="json")
            self.assertEqual(r.status_code, 404, who)
        delay.assert_not_called()
        # A name is a name, never a path.
        r = api(self.tok_a1).patch(f"/api/v1/documents/files/{self.link_a1.id}/rename/",
                                   {"new_name": "../../../etc/cron.d/evil"}, format="json")
        self.assertEqual(r.status_code, 202)
        delay.assert_called_once_with(self.file_a1.id, "evil", actor_user_id=self.a1.pk,
                                      expected_updated_at=self.file_a1.updated_at.isoformat())

    def test_safe_filename(self):
        self.assertEqual(access.safe_filename("..\\..\\windows\\x.txt"), "x.txt")
        for bad in ("", "..", "/", "a/..", None):
            with self.assertRaises(ValueError):
                access.safe_filename(bad)

    def test_file_endpoints_hide_other_peoples_files(self):
        lid = self.link_a1.id
        calls = [
            ("get", f"/api/v1/documents/files/{lid}/", None),
            ("get", f"/api/v1/documents/files/{lid}/preview/", None),
            ("get", f"/api/v1/documents/files/{lid}/audit/", None),
            ("get", f"/api/v1/documents/files/{lid}/versions/", None),
            ("get", f"/api/v1/documents/files/{lid}/aliases/", None),
            ("post", f"/api/v1/documents/files/{lid}/duplicate/", {}),
            ("post", f"/api/v1/documents/files/{lid}/protect/", {"password_hint": "x"}),
            ("post", f"/api/v1/documents/files/{lid}/restore/", {}),
            ("post", f"/api/v1/documents/files/{lid}/share/", {"emails": ["b1@firm-b.test"]}),
            ("post", f"/api/v1/documents/files/{lid}/share/public/", {}),
            ("post", f"/api/v1/documents/files/{lid}/unshare/", {}),
            ("post", f"/api/v1/documents/files/{lid}/color/", {"color": "red"}),
            ("post", f"/api/v1/documents/files/{lid}/aliases/", {"alias_name": "x"}),
            ("post", "/api/v1/documents/files/zip/", {"file_ids": [lid]}),
            ("patch", f"/api/v1/documents/files/{lid}/trash/", {}),
            ("delete", f"/api/v1/documents/files/{lid}/delete/", None),
        ]
        with mock.patch("celery.app.task.Task.delay") as delay:
            for who, tok in self.outsiders():
                for method, url, body in calls:
                    r = getattr(api(tok), method)(url, body, format="json") if body is not None \
                        else getattr(api(tok), method)(url)
                    self.assertEqual(r.status_code, 404, f"{who}: {method.upper()} {url} -> {r.status_code}")
            delay.assert_not_called()
        self.assertTrue(File.objects.filter(pk=self.file_a1.pk).exists())
        self.assertFalse(self.link_a1.aliases.exists())
        self.assert_untouched()

    def test_folder_endpoints_hide_other_peoples_folders(self):
        fid = str(self.folder_a1.id)
        calls = [
            ("get", f"/api/v1/documents/folders/{fid}/", None),
            ("patch", f"/api/v1/documents/folders/{fid}/rename/", {"new_name": "Stolen"}),
            ("patch", f"/api/v1/documents/folders/{fid}/trash/", {}),
            ("post", f"/api/v1/documents/folders/{fid}/restore/", {}),
            ("delete", f"/api/v1/documents/folders/{fid}/delete/", None),
            ("post", f"/api/v1/documents/folders/{fid}/color/", {"color": "red"}),
            ("post", f"/api/v1/documents/folders/{fid}/aliases/", {"alias_name": "x"}),
            ("post", "/api/v1/documents/folders/bulk-trash/", {"folder_ids": [fid]}),
            ("post", "/api/v1/documents/folders/bulk-restore/", {"folder_ids": [fid]}),
            ("post", "/api/v1/documents/folders/", {"name": "child", "project_id": "p", "service_id": "s", "parent": fid}),
        ]
        with mock.patch("celery.app.task.Task.delay") as delay:
            for who, tok in [("colleague", self.tok_a2), ("other organisation", self.tok_b1)]:
                for method, url, body in calls:
                    r = getattr(api(tok), method)(url, body, format="json") if body is not None \
                        else getattr(api(tok), method)(url)
                    self.assertEqual(r.status_code, 404, f"{who}: {method.upper()} {url} -> {r.status_code}")
            # An admin of ANOTHER organisation cannot bulk-delete this organisation's folders.
            r = api(self.tok_badmin).post("/api/v1/documents/folders/bulk-delete/", {"folder_ids": [fid]}, format="json")
            self.assertEqual(r.status_code, 404)
            delay.assert_not_called()
        self.assertFalse(Folder.objects.filter(parent=self.folder_a1).exists())
        self.assert_untouched()

    def test_owner_and_own_admin_keep_access(self):
        self.assertEqual(api(self.tok_a1).get(f"/api/v1/documents/files/{self.link_a1.id}/").status_code, 200)
        self.assertEqual(api(self.tok_admin).get(f"/api/v1/documents/files/{self.link_a1.id}/").status_code, 200)
        r = api(self.tok_a1).patch(f"/api/v1/documents/folders/{self.folder_a1.id}/trash/", {}, format="json")
        self.assertEqual(r.status_code, 200)

    def test_metadata_is_only_for_people_who_can_open_the_file(self):
        Metadata.objects.create(file=self.file_a1, author="Confidential Author")
        for who, tok in self.outsiders():
            r = api(tok).get(f"/api/v1/core/metadata-status/?file_id={self.file_a1.id}")
            self.assertEqual(r.status_code, 404, who)
        r = api(self.tok_a1).get(f"/api/v1/core/metadata-status/?file_id={self.file_a1.id}")
        self.assertEqual(r.status_code, 200)


@override_settings(**NO_SEARCH)
class ServiceIsolationTests(Base):
    def test_redline_results_follow_the_source_document(self):
        from document_redlining.models import RedliningResult, RedliningRun

        run = RedliningRun.objects.create(project_id="p1", service_id="s1", client_name=self.a1.username, status="Completed")
        RedliningResult.objects.create(run=run, original_file=self.file_a1, diff_html="<p>privileged</p>", status="Completed")
        for who, tok in self.outsiders():
            r = api(tok).get(f"/api/v1/redlining/download-redlining-result/?file_id={self.file_a1.id}")
            self.assertEqual(r.status_code, 404, who)
            r = api(tok).get(f"/api/v1/redlining/check-redlining-status/?run_id={run.id}")
            self.assertEqual(r.status_code, 404, who)
            self.assertNotIn("privileged", r.content.decode())
        r = api(self.tok_a1).get(f"/api/v1/redlining/download-redlining-result/?file_id={self.file_a1.id}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("privileged", r.json()["diff_html"])
        r = api(self.tok_a1).get(f"/api/v1/redlining/check-redlining-status/?run_id={run.id}")
        self.assertEqual(r.status_code, 200)

    def test_clause_library_is_not_shared_between_organisations(self):
        from document_automation.models import Clause

        mine = Clause.objects.create(title="Firm A indemnity", content="secret wording", project_id="p1",
                                     service_id="s1", client_name=self.a1.username)
        r = api(self.tok_b1).get("/api/v1/automation/clauses/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("secret wording", r.content.decode())
        self.assertEqual(api(self.tok_b1).get(f"/api/v1/automation/clauses/{mine.id}/").status_code, 404)
        self.assertEqual(api(self.tok_b1).delete(f"/api/v1/automation/clauses/{mine.id}/").status_code, 404)
        self.assertTrue(Clause.objects.filter(pk=mine.pk).exists())
        self.assertIn("secret wording", api(self.tok_a1).get("/api/v1/automation/clauses/").content.decode())
        # The organisation's own admin sees it; a colleague's private library stays private.
        self.assertIn("secret wording", api(self.tok_admin).get("/api/v1/automation/clauses/").content.decode())
        self.assertNotIn("secret wording", api(self.tok_a2).get("/api/v1/automation/clauses/").content.decode())

    def test_retention_policy_cannot_be_changed_from_outside(self):
        from document_versioning.models import VersionRetentionPolicy

        url = "/api/v1/versioning/retention-policy/?project_id=p1"
        hdr = lambda app: {"HTTP_X_CLIENT_ID": app.client_id, "HTTP_X_CLIENT_SECRET": "x"}  # noqa: E731
        r = api(self.tok_b1, **hdr(self.app_b1)).put(url, {"max_versions": 1, "retention_days": 1}, format="json")
        self.assertEqual(r.status_code, 403)
        r = api(self.tok_a1, **hdr(self.app_a1)).put(url, {"max_versions": 1, "retention_days": 1}, format="json")
        self.assertEqual(r.status_code, 403)  # not an admin
        self.assertFalse(VersionRetentionPolicy.objects.filter(project_id="p1").exists())
        r = api(self.tok_admin, **hdr(self.app_admin)).put(url, {"max_versions": 5, "retention_days": 30}, format="json")
        self.assertIn(r.status_code, (200, 201))

    def test_email_outbox_is_for_platform_administrators_only(self):
        for tok in (self.tok_a1, self.tok_admin):
            self.assertEqual(api(tok).get("/api/v1/email/outbox/").status_code, 403)
            self.assertEqual(api(tok).get("/api/v1/email/templates/").status_code, 403)
            r = api(tok).post("/api/v1/email/outbox/", {"to": ["victim@example.com"], "subject": "x"}, format="json")
            self.assertEqual(r.status_code, 403)
        root = make_user("root@platform.test", self.firm_a, superuser=True, staff=True)
        _, tok = token_for(root)
        self.assertEqual(api(tok).get("/api/v1/email/outbox/").status_code, 200)

    def test_search_index_administration_is_platform_only(self):
        with mock.patch("celery.app.task.Task.delay") as delay:
            for url in ("/api/v1/es/delete-index/", "/api/v1/es/force-reindex/", "/api/v1/document-search/reindex-missing/"):
                r = api(self.tok_admin).post(url, {}, format="json")
                self.assertIn(r.status_code, (403, 404), url)
            delay.assert_not_called()


@override_settings(**NO_SEARCH)
class AuthenticationTests(Base):
    def setUp(self):
        super().setUp()
        patcher = override_settings(REST_FRAMEWORK={**__import__("django.conf").conf.settings.REST_FRAMEWORK,
                                                    "DEFAULT_THROTTLE_RATES": OPEN_RATES})
        patcher.enable()
        self.addCleanup(patcher.disable)
        from rest_framework.throttling import SimpleRateThrottle
        saved = SimpleRateThrottle.THROTTLE_RATES
        SimpleRateThrottle.THROTTLE_RATES = OPEN_RATES
        self.addCleanup(lambda: setattr(SimpleRateThrottle, "THROTTLE_RATES", saved))

    @mock.patch("custom_authentication.views.send_password_reset_email_task.delay")
    def test_reset_token_goes_to_the_mailbox_only(self, delay):
        r = APIClient().post("/api/v1/auth/password/request-reset/", {"email": "a1@firm-a.test"}, format="json")
        self.assertEqual(r.status_code, 200)
        token = PasswordResetToken.objects.get(user=self.a1, is_used=False)
        self.assertNotIn(str(token.token), r.content.decode())
        self.assertNotIn("reset_token", r.json())
        delay.assert_called_once()
        # An address without an account gets exactly the same answer.
        r2 = APIClient().post("/api/v1/auth/password/request-reset/", {"email": "nobody@nowhere.test"}, format="json")
        self.assertEqual((r2.status_code, r2.json()), (r.status_code, r.json()))

    @mock.patch("custom_authentication.views.send_password_reset_success_email_task.delay")
    def test_reset_token_is_single_use_expires_and_signs_everyone_out(self, _):
        token = PasswordResetToken.objects.create(user=self.a1)
        body = {"token": str(token.token), "new_password": "A-much-better-Passphrase-42"}
        self.assertEqual(APIClient().post("/api/v1/auth/password/reset/", body, format="json").status_code, 200)
        self.a1.refresh_from_db()
        self.assertTrue(self.a1.check_password("A-much-better-Passphrase-42"))
        self.assertFalse(AccessToken.objects.filter(user=self.a1).exists())
        body["new_password"] = "Another-good-Passphrase-43"
        self.assertEqual(APIClient().post("/api/v1/auth/password/reset/", body, format="json").status_code, 400)
        old = PasswordResetToken.objects.create(user=self.a1)
        PasswordResetToken.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(hours=2))
        r = APIClient().post("/api/v1/auth/password/reset/", {"token": str(old.token), "new_password": "Yet-another-Passphrase-44"}, format="json")
        self.assertEqual(r.status_code, 400)
        r = APIClient().post("/api/v1/auth/password/reset/", {"token": "not-a-token", "new_password": "x"}, format="json")
        self.assertEqual(r.status_code, 400)

    @mock.patch("custom_authentication.views.send_password_reset_success_email_task.delay")
    def test_reset_rejects_weak_passwords(self, _):
        token = PasswordResetToken.objects.create(user=self.a1)
        r = APIClient().post("/api/v1/auth/password/reset/", {"token": str(token.token), "new_password": "12345"}, format="json")
        self.assertEqual(r.status_code, 400)
        token.refresh_from_db()
        self.assertFalse(token.is_used)

    @mock.patch("custom_authentication.views.send_signup_email_task.delay")
    def test_signup_cannot_join_an_existing_organisation(self, delay):
        body = {"email": "intruder@evil.test", "password": "A-decent-Passphrase-1", "organisation": "firm a",
                "contact_name": "Mallory", "contact_phone": "000"}
        r = APIClient().post("/api/v1/auth/signup/", body, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(User.objects.filter(email="intruder@evil.test").exists())
        body["organisation"] = "Brand New Firm"
        r = APIClient().post("/api/v1/auth/signup/", body, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertNotEqual(User.objects.get(email="intruder@evil.test").client_id, self.firm_a.id)

    def _login(self, password="pw-Test-12345", secret=None, app=None):
        app = app or self.app_a1
        return APIClient().post("/api/v1/auth/login/", {"email": "a1@firm-a.test", "password": password}, format="json",
                                HTTP_X_CLIENT_ID=app.client_id, HTTP_X_CLIENT_SECRET=secret or "wrong")

    def test_login_verifies_the_client_secret(self):
        app = Application.objects.create(user=self.a1, client_type=Application.CLIENT_CONFIDENTIAL,
                                         authorization_grant_type=Application.GRANT_PASSWORD, name="login",
                                         client_secret="the-real-secret")
        self.assertEqual(self._login(secret="a-guess", app=app).status_code, 401)
        r = self._login(secret="the-real-secret", app=app)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("access_token", r.json())

    @override_settings(AUTH_MAX_FAILED_LOGINS=3)
    def test_repeated_failures_pause_the_account(self):
        app = Application.objects.create(user=self.a1, client_type=Application.CLIENT_CONFIDENTIAL,
                                         authorization_grant_type=Application.GRANT_PASSWORD, name="login2",
                                         client_secret="the-real-secret")
        for _ in range(3):
            self.assertEqual(self._login(password="nope", app=app).status_code, 401)
        self.assertEqual(self._login(secret="the-real-secret", app=app).status_code, 429)
        cache.clear()
        self.assertEqual(self._login(secret="the-real-secret", app=app).status_code, 200)

    def test_token_refresh_needs_the_client_secret(self):
        app = Application.objects.create(user=self.a1, client_type=Application.CLIENT_CONFIDENTIAL,
                                         authorization_grant_type=Application.GRANT_PASSWORD, name="refresh",
                                         client_secret="the-real-secret")
        tok = AccessToken.objects.create(user=self.a1, application=app, token="refresh-me",
                                         expires=timezone.now() + timedelta(hours=1), scope="read write")
        post = lambda secret: APIClient().post(  # noqa: E731
            "/api/v1/auth/token/refresh/", {"refresh_token": tok.token}, format="json",
            HTTP_X_CLIENT_ID=app.client_id, HTTP_X_CLIENT_SECRET=secret)
        self.assertEqual(post("a-guess").status_code, 401)
        r = post("the-real-secret")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(AccessToken.objects.filter(token="refresh-me").exists())  # rotated
        self.assertEqual(post("the-real-secret").status_code, 401)

    def test_api_keys_can_only_be_revoked_by_their_owner_or_their_admin(self):
        for tok in (self.tok_a2, self.tok_b1, self.tok_badmin):
            r = api(tok).delete("/api/v1/auth/user/api-keys/revoke/", {"client_id": self.app_a1.client_id}, format="json")
            self.assertEqual(r.status_code, 404)
        self.assertTrue(Application.objects.filter(pk=self.app_a1.pk).exists())
        r = api(self.tok_admin).delete("/api/v1/auth/user/api-keys/revoke/", {"client_id": self.app_a2.client_id}, format="json")
        self.assertEqual(r.status_code, 200)

    def test_org_admin_cannot_create_a_platform_admin(self):
        from django.urls import reverse
        url = reverse("admin_users_create")
        body = {"email": "mole@firm-a.test", "organisation": "Firm A", "contact_name": "M", "contact_phone": "0",
                "role": "SuperAdmin"}
        with mock.patch("celery.app.task.Task.delay"):
            r = api(self.tok_admin).post(url, body, format="json")
            self.assertEqual(r.status_code, 403)
            self.assertFalse(User.objects.filter(email="mole@firm-a.test").exists())
            # ...and an ordinary colleague is created in the admin's own organisation, whatever name is sent.
            body.update(role="Client", email="new@firm-a.test", organisation="FIRM A")
            r = api(self.tok_admin).post(url, body, format="json")
            self.assertEqual(r.status_code, 201, r.content)
        created = User.objects.get(email="new@firm-a.test")
        self.assertEqual(created.client_id, self.firm_a.id)
        self.assertFalse(access.is_platform_superuser(created))

    def test_client_secrets_of_another_organisation_are_hidden(self):
        r = api(self.tok_badmin).get(f"/api/v1/auth/admin/users/{self.a1.id}/oauth-credentials/")
        self.assertEqual(r.status_code, 404)
        r = api(self.tok_admin).get(f"/api/v1/auth/admin/users/{self.a1.id}/oauth-credentials/")
        self.assertEqual(r.status_code, 200)


class ThrottleTests(TestCase):
    def test_sign_in_is_rate_limited_per_address(self):
        from rest_framework.throttling import SimpleRateThrottle
        saved = SimpleRateThrottle.THROTTLE_RATES
        SimpleRateThrottle.THROTTLE_RATES = {**saved, "auth": "3/min"}
        self.addCleanup(lambda: setattr(SimpleRateThrottle, "THROTTLE_RATES", saved))
        cache.clear()
        codes = [APIClient().post("/api/v1/auth/login/", {"email": "x@y.test", "password": "z"}, format="json",
                                  HTTP_X_CLIENT_ID="c", HTTP_X_CLIENT_SECRET="s").status_code for _ in range(5)]
        self.assertEqual(codes[-1], 429)
        self.assertNotIn(429, codes[:3])

    def _rates(self, **rates):
        from rest_framework.throttling import SimpleRateThrottle
        saved = SimpleRateThrottle.THROTTLE_RATES
        SimpleRateThrottle.THROTTLE_RATES = {**saved, **rates}
        self.addCleanup(lambda: setattr(SimpleRateThrottle, "THROTTLE_RATES", saved))
        cache.clear()

    def _attempt(self, email):
        return APIClient().post("/api/v1/auth/login/", {"email": email, "password": "z"}, format="json",
                                HTTP_X_CLIENT_ID="c", HTTP_X_CLIENT_SECRET="s").status_code

    def test_an_office_behind_one_address_can_sign_in_together(self):
        """500 colleagues share one office address: each account has its own allowance."""
        self._rates(auth="3/min", auth_network="1000/min")
        codes = [self._attempt(f"person{i}@firm.test") for i in range(40)]
        self.assertNotIn(429, codes)

    def test_one_address_spraying_many_accounts_is_still_stopped(self):
        self._rates(auth="3/min", auth_network="10/min")
        codes = [self._attempt(f"person{i}@firm.test") for i in range(12)]
        self.assertNotIn(429, codes[:10])
        self.assertEqual(codes[-1], 429)


class DeploymentSafetyTests(TestCase):
    def test_production_defaults(self):
        from django.conf import settings
        self.assertFalse(settings.DEBUG)
        self.assertFalse(settings.CELERY_TASK_REJECT_ON_WORKER_LOST)   # a killed task is not re-queued forever
        self.assertGreater(settings.CELERY_TASK_TIME_LIMIT, settings.CELERY_TASK_SOFT_TIME_LIMIT)
        self.assertGreater(settings.CELERY_BROKER_TRANSPORT_OPTIONS["visibility_timeout"], settings.CELERY_TASK_TIME_LIMIT)
        self.assertLessEqual(settings.CELERY_BROKER_TRANSPORT_OPTIONS["socket_connect_timeout"], 10)
        self.assertLessEqual(settings.CELERY_TASK_PUBLISH_RETRY_POLICY["max_retries"], 3)
        self.assertIn("auth", settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"])

    def test_api_docs_are_not_public_by_default(self):
        self.assertEqual(APIClient().get("/api/v1/swagger/").status_code, 404)
        self.assertEqual(APIClient().get("/api/v1/health/").status_code, 200)

    def test_interactive_work_has_its_own_queue(self):
        from aiDocuMines.celery_app import app
        route = lambda name: app.amqp.router.route({}, name)["queue"].name  # noqa: E731
        self.assertEqual(route("custom_authentication.tasks.send_password_reset_email_task"), "ai_documines_fast")
        self.assertEqual(route("grid_documents_interrogation.tasks.process_file_query"), "ai_documines_fast")
        self.assertEqual(route("document_ocr.tasks.process_ocr"), "ai_documines")
        self.assertIn("core.maintenance.fail_stale_work", app.tasks)


@override_settings(**NO_SEARCH)
class StaleWorkTests(Base):
    def test_work_with_no_progress_is_reported_as_failed(self):
        from document_ocr.models import OCRRun

        stuck = OCRRun.objects.create(project_id="p", service_id="s", client=self.firm_a, client_name="a1", status="Processing")
        fresh = OCRRun.objects.create(project_id="p", service_id="s", client=self.firm_a, client_name="a1", status="Processing")
        done = OCRRun.objects.create(project_id="p", service_id="s", client=self.firm_a, client_name="a1", status="Completed")
        OCRRun.objects.filter(pk__in=[stuck.pk, done.pk]).update(updated_at=timezone.now() - timedelta(days=2))
        report = maintenance.fail_stale()
        self.assertEqual(report.get("document_ocr.OCRRun"), 1)
        for run in (stuck, fresh, done):
            run.refresh_from_db()
        self.assertEqual((stuck.status, fresh.status, done.status), ("Failed", "Processing", "Completed"))
        self.assertIn("run it again", stuck.error_message)
        # After a queue purge nothing may stay "Processing".
        maintenance.fail_stale(everything=True)
        fresh.refresh_from_db()
        self.assertEqual(fresh.status, "Failed")


@override_settings(**NO_SEARCH)
class RemainingAuditTests(Base):
    def test_break_glass_routes_are_absent_even_when_enabled(self):
        from django.urls import Resolver404, resolve
        import os
        with mock.patch.dict(os.environ, {"ENABLE_BREAK_GLASS_ENDPOINTS": "true"}):
            for path in ("supersuperuser-reset", "supersuperuser-clean-db"):
                with self.assertRaises(Resolver404):
                    resolve(f"/api/v1/auth/admin/{path}/")

    def test_only_admins_can_write_organisation_settings(self):
        from django.urls import reverse
        for name in ("system_settings_post", "system_settings_reset"):
            response = api(self.tok_a1, HTTP_X_CLIENT_ID=self.app_a1.client_id).post(
                reverse(name), {}, format="json")
            self.assertEqual(response.status_code, 403)
        response = api(self.tok_admin, HTTP_X_CLIENT_ID=self.app_admin.client_id).post(
            reverse("system_settings_post"), {}, format="json")
        self.assertEqual(response.status_code, 200, response.content)


@override_settings(**NO_SEARCH)
class AsyncResultIsolationTests(Base):
    def test_task_results_are_only_readable_by_the_initiator(self):
        from core.task_access import register_task
        from document_anonymizer.views import DocumentRiskScoreResultView, AnonymizationStatsResultView
        from private_equity.views import PETaskStatusView
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()
        cases = [(DocumentRiskScoreResultView, "risk", "document_anonymizer.views.AsyncResult"),
                 (AnonymizationStatsResultView, "anonymization_stats", "document_anonymizer.views.AsyncResult"),
                 (PETaskStatusView, "private_equity", "celery.result.AsyncResult")]
        for view, kind, target in cases:
            task = mock.Mock(id=f"owned-{kind}")
            register_task(self.a1, task, kind)
            with mock.patch(target) as result:
                result.return_value.ready.return_value = False
                result.return_value.state = "PENDING"
                result.return_value.status = "PENDING"
                for user in (self.a2, self.b1, self.b_admin):
                    req = factory.get("/", {"task_id": task.id})
                    force_authenticate(req, user=user, token=self.tok_a1)
                    self.assertEqual(view.as_view()(req).status_code, 404)
                result.assert_not_called()
                req = factory.get("/", {"task_id": task.id})
                force_authenticate(req, user=self.a1, token=self.tok_a1)
                self.assertIn(view.as_view()(req).status_code, (200, 202))
                result.assert_called_once_with(task.id)

    @mock.patch("document_anonymizer.views.compute_risk_score_task.delay")
    def test_risk_score_cannot_be_started_for_foreign_file(self, delay):
        from document_anonymizer.views import DocumentRiskScoreView
        from rest_framework.test import APIRequestFactory, force_authenticate
        req = APIRequestFactory().get("/", {"file_id": self.file_a1.pk})
        force_authenticate(req, user=self.b1, token=self.tok_b1)
        self.assertEqual(DocumentRiskScoreView.as_view()(req).status_code, 404)
        delay.assert_not_called()


@override_settings(**NO_SEARCH, SPA_REDIRECT_URIS=["https://app.test/sign_in"])
class BrowserPKCETests(Base):
    def authorize(self, **extra):
        import base64, hashlib
        self.verifier = "v" * 43
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        return APIClient().post("/api/v1/auth/browser/authorize/", {
            "email": self.a1.email, "password": "pw-Test-12345", "code_challenge": challenge,
            "code_challenge_method": "S256", "redirect_uri": "https://app.test/sign_in", **extra,
        }, format="json")

    def exchange(self, grant, **extra):
        return APIClient().post("/o/token/", {"grant_type": "authorization_code", "code": grant["code"],
            "client_id": grant["client_id"], "redirect_uri": "https://app.test/sign_in",
            "code_verifier": self.verifier, **extra})

    def test_public_code_exchange_and_replay_rejection(self):
        result = self.authorize()
        self.assertEqual(result.status_code, 200, result.content)
        self.assertNotIn("client_secret", result.json())
        grant = result.json()
        result = self.exchange(grant)
        self.assertEqual(result.status_code, 200, result.content)
        token = AccessToken.objects.get(token=result.json()["access_token"])
        self.assertEqual(token.user, self.a1)
        self.assertEqual(token.application.client_type, Application.CLIENT_PUBLIC)
        self.assertEqual(self.exchange(grant).status_code, 400)

    def test_wrong_verifier_and_redirect_are_rejected(self):
        grant = self.authorize().json()
        self.assertEqual(self.exchange(grant, code_verifier="x" * 43).status_code, 400)
        self.assertEqual(self.exchange(grant, redirect_uri="https://evil.test/sign_in").status_code, 400)

    def test_expired_code_is_rejected(self):
        from oauth2_provider.models import Grant
        grant = self.authorize().json()
        Grant.objects.filter(code=grant["code"]).update(expires=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.exchange(grant).status_code, 400)

    def test_password_redirect_and_s256_are_required(self):
        self.assertEqual(self.authorize(password="wrong").status_code, 401)
        self.assertEqual(self.authorize(redirect_uri="https://evil.test/sign_in").status_code, 400)
        self.assertEqual(self.authorize(code_challenge_method="plain").status_code, 400)

    @mock.patch("custom_authentication.browser_auth.send_signin_code_task.delay")
    def test_two_factor_cannot_be_bypassed(self, mail):
        from custom_authentication.models import TwoFactorCode
        self.a1.is_2fa_enabled = True
        self.a1.save()
        result = self.authorize()
        self.assertTrue(result.json()["requires_2fa"])
        self.assertNotIn("code", result.json())
        self.assertEqual(self.authorize(**{"2fa_code": "000000"}).status_code, 401)
        code = TwoFactorCode.objects.get(user=self.a1, is_used=False).code
        self.assertIn("code", self.authorize(**{"2fa_code": code}).json())
        self.assertEqual(self.authorize(**{"2fa_code": code}).status_code, 401)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_invitation_contains_only_one_use_link(self):
        from custom_authentication.tasks import send_signup_email_task
        from django.core import mail
        send_signup_email_task(self.a1.pk)
        body = mail.outbox[0].body
        self.assertIn("/set-password?token=", body)
        self.assertNotIn("pw-Test-12345", body)
        self.assertNotIn("Client Secret", body)
        self.assertEqual(PasswordResetToken.objects.filter(user=self.a1, is_used=False).count(), 1)


class QueuePurgeIsolationTests(TestCase):
    @mock.patch("core.maintenance.fail_stale", return_value={})
    @mock.patch("core.maintenance._broker")
    def test_purge_preserves_other_applications_reserved_jobs(self, broker, stale):
        import json
        redis = broker.return_value
        redis.llen.return_value = 2
        redis.hscan_iter.return_value = [
            (b"mine", json.dumps([{}, "", "ai_documines"])),
            (b"fast", json.dumps([{}, "", "ai_documines_fast"])),
            (b"foreign", json.dumps([{}, "", "another_app"])),
            (b"unknown", "malformed data"),
        ]
        report = maintenance.purge_queues()
        self.assertEqual(report["reserved_by_workers"], 2)
        self.assertEqual(redis.delete.call_args_list, [mock.call("ai_documines"), mock.call("ai_documines_fast")])
        redis.pipeline.return_value.hdel.assert_called_once_with("unacked", b"mine", b"fast")
        redis.pipeline.return_value.zrem.assert_called_once_with("unacked_index", b"mine", b"fast")
        stale.assert_called_once_with(everything=True)


@override_settings(**NO_SEARCH)
class DocumentDeliveryTests(Base):
    """Opening a document must never make the browser download or run it."""

    def _stored(self, name, content=b"x"):
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, name)
        with open(path, "wb") as fh:
            fh.write(content)
        f = make_file(self.a1, name, path=path)
        link = FileFolderLink.objects.create(file=f, folder=self.folder_a1)
        return f, link

    def test_office_files_are_not_previewed(self):
        _, link = self._stored("contract.docx", b"PK\x03\x04 docx bytes")
        r = api(self.tok_a1).get(f"/api/v1/documents/files/{link.id}/preview/")
        self.assertEqual(r.status_code, 415)
        self.assertNotIn("attachment", r.get("Content-Disposition", ""))

    def test_pdf_preview_has_a_fixed_type(self):
        _, link = self._stored("terms.pdf", b"%PDF-1.4 minimal")
        r = api(self.tok_a1).get(f"/api/v1/documents/files/{link.id}/preview/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        self.assertTrue(r["Content-Disposition"].startswith("inline"))

    def test_html_is_never_served_inline(self):
        f, link = self._stored("evil.html", b"<script>alert(1)</script>")
        self.assertEqual(api(self.tok_a1).get(f"/api/v1/documents/files/{link.id}/preview/").status_code, 415)
        r = api(self.tok_a1).get(f"/api/v1/core/download/?file_id={f.id}&inline=true")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r["Content-Disposition"].startswith("attachment"))

    def test_word_bytes_still_reach_the_in_app_preview(self):
        f, _ = self._stored("brief.docx", b"PK\x03\x04 docx bytes")
        r = api(self.tok_a1).get(f"/api/v1/core/download/?file_id={f.id}&inline=true")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b"".join(r.streaming_content), b"PK\x03\x04 docx bytes")

    def test_other_organisations_cannot_preview(self):
        _, link = self._stored("terms.pdf", b"%PDF-1.4 minimal")
        self.assertEqual(api(self.tok_b1).get(f"/api/v1/documents/files/{link.id}/preview/").status_code, 404)
