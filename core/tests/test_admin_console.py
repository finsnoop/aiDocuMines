"""Admin console API: organisation scoping, safe user creation, enforced settings, audit and backups."""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.tests.test_tenancy import make_user
from custom_authentication.models import AdminAuditEvent, Client, CustomUser, OrganisationPolicy

A = "/api/v1/auth/admin/"


def api(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


class AdminConsoleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Firm A")
        cls.other = Client.objects.create(name="Firm B")
        cls.admin = make_user("admin@firm-a.test", cls.firm, staff=True, groups=["Admin"])
        cls.member = make_user("member@firm-a.test", cls.firm, groups=["Client"])
        cls.outsider = make_user("x@firm-b.test", cls.other, groups=["Client"])
        cls.platform = Client.objects.create(name="Platform")
        cls.root = make_user("root@platform.test", cls.platform, staff=True, superuser=True)

    def setUp(self):
        cache.clear()

    # people
    def test_people_scoped_to_own_organisation(self):
        r = api(self.admin).get(A + "people/").json()
        emails = {p["email"] for p in r["results"]}
        self.assertIn("member@firm-a.test", emails)
        self.assertNotIn("x@firm-b.test", emails)
        self.assertNotIn("SuperAdmin", r["grantable_roles"])

    def test_non_admin_is_refused(self):
        self.assertEqual(api(self.member).get(A + "people/").status_code, 403)
        self.assertEqual(api(self.member).post(A + "people/", {"email": "n@x.test"}, format="json").status_code, 403)

    @mock.patch("custom_authentication.views_admin.requests.post")
    def test_admin_adds_user_to_own_org_and_sign_in_is_registered(self, post):
        post.return_value = mock.Mock(status_code=201)
        r = api(self.admin).post(A + "people/", {"email": "New@Firm-A.test", "name": "New Person", "roles": ["Manager"],
                                                  "organisation_id": self.other.id}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        u = CustomUser.objects.get(email="new@firm-a.test")
        self.assertEqual(u.client_id, self.firm.id)                    # organisation_id ignored for org admins
        self.assertIn("Manager", r.json()["roles"])
        self.assertTrue(r.json()["temporary_password"])
        self.assertTrue(r.json()["sign_in_ready"])
        self.assertIn("/api/user", post.call_args[0][0])
        self.assertTrue(AdminAuditEvent.objects.filter(action="user.created", target_label="new@firm-a.test").exists())

    @mock.patch("custom_authentication.views_admin.requests.post", return_value=mock.Mock(status_code=201))
    def test_cannot_grant_platform_role(self, _post):
        r = api(self.admin).post(A + "people/", {"email": "a@firm-a.test", "roles": ["SuperAdmin"]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(CustomUser.objects.filter(email="a@firm-a.test").exists())

    def test_cannot_manage_other_firm_or_disable_self(self):
        self.assertEqual(api(self.admin).patch(A + f"people/{self.outsider.id}/", {"is_active": False},
                                               format="json").status_code, 404)
        self.assertEqual(api(self.admin).patch(A + f"people/{self.admin.id}/", {"is_active": False},
                                               format="json").status_code, 400)

    def test_disable_signs_user_out(self):
        from core.tests.test_tenancy import token_for
        from oauth2_provider.models import AccessToken
        token_for(self.member)
        r = api(self.admin).patch(A + f"people/{self.member.id}/", {"is_active": False}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(AccessToken.objects.filter(user=self.member).exists())

    @mock.patch("custom_authentication.views_admin.requests.post", return_value=mock.Mock(status_code=200))
    def test_reset_password(self, post):
        r = api(self.admin).post(A + f"people/{self.member.id}/reset-password/")
        self.assertEqual(r.status_code, 200)
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password(r.json()["temporary_password"]))
        self.assertIn("updateUserPasswordByEmail", post.call_args[0][0])

    def test_reset_password_registers_someone_the_sign_in_service_lost(self):
        # Sign-in service does not know the person (404), then accepts the registration (201).
        responses = [mock.Mock(status_code=404), mock.Mock(status_code=201)]
        with mock.patch("custom_authentication.views_admin.requests.post", side_effect=responses) as post:
            r = api(self.admin).post(A + f"people/{self.member.id}/reset-password/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["sign_in_updated"])
        register = post.call_args_list[1]
        self.assertTrue(register[0][0].endswith("/api/user"))
        self.assertEqual(register[1]["json"]["email"], self.member.email)
        self.assertEqual(register[1]["json"]["password"], r.json()["temporary_password"])

    # organisations
    def test_org_admin_sees_only_own_org_and_cannot_create(self):
        r = api(self.admin).get(A + "organisations/").json()
        self.assertEqual([o["name"] for o in r["results"]], ["Firm A"])
        self.assertEqual(api(self.admin).post(A + "organisations/", {"name": "New"}, format="json").status_code, 403)
        self.assertEqual(api(self.admin).get(A + f"clients/{self.other.id}/").status_code, 404)   # old endpoint leak fixed

    def test_platform_admin_creates_organisation(self):
        r = api(self.root).post(A + "organisations/", {"name": "Firm C", "industry": "Legal"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertTrue(OrganisationPolicy.objects.filter(client__name="Firm C").exists())

    # settings that take effect
    def test_policy_changes_are_enforced_on_upload(self):
        from core.models import Matter
        from django.core.files.uploadedfile import SimpleUploadedFile
        import tempfile
        r = api(self.admin).patch(A + "policy/", {"allowed_file_types": ".PDF, docx"}, format="json")
        self.assertEqual(r.json()["allowed_file_types"], "docx,pdf")
        m = Matter.objects.create(client=self.firm, name="M1", created_by=self.member)
        m.members.add(self.member)
        exe = SimpleUploadedFile("tool.exe", b"MZ...", content_type="application/octet-stream")
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            res = api(self.member).post(f"/api/v1/core/projects/{m.id}/upload/", {"file": [exe]}, format="multipart")
        self.assertEqual(res.json()["files"][0]["status"], "rejected")
        self.assertIn("does not allow", res.json()["files"][0]["message"])

    def test_assistant_switch_is_enforced(self):
        api(self.admin).patch(A + "policy/", {"assistant_enabled": False}, format="json")
        r = api(self.member).get("/api/v1/core/assistant/status/").json()
        self.assertFalse(r["assistant"])

    def test_policy_validation_and_audit(self):
        self.assertEqual(api(self.admin).patch(A + "policy/", {"max_upload_mb": 0}, format="json").status_code, 400)
        api(self.admin).patch(A + "policy/", {"session_timeout_minutes": 45}, format="json")
        events = api(self.admin).get(A + "audit/?type=admin").json()["results"]
        self.assertTrue(any(e["action"] == "settings.updated" for e in events))
        self.assertFalse(any(e["actor"] == "x@firm-b.test" for e in events))

    # roles
    def test_role_matrix(self):
        r = api(self.admin).get(A + "role-matrix/").json()
        client_role = next(x for x in r["roles"] if x["name"] == "Client")
        self.assertIn("assistant.ask", client_role["permissions"])
        self.assertIn("document.view", client_role["permissions"])   # inherited from Guest

    # backups
    def test_backups_hidden_from_org_admins_and_restore_guarded(self):
        self.assertEqual(api(self.admin).get("/api/v1/core/backups/").status_code, 404)
        import tempfile
        with mock.patch.dict("os.environ", {"BACKUP_DIR": tempfile.mkdtemp(), "ENABLE_BACKUP_RESTORE": "false"}):
            self.assertEqual(api(self.root).get("/api/v1/core/backups/").status_code, 200)
            with mock.patch("core.views_backup.create_database_backup.delay") as delay:
                r = api(self.root).post("/api/v1/core/backups/")
            self.assertEqual(r.status_code, 202)
            delay.assert_called_once()
            name = r.json()["name"]
            import os
            open(os.path.join(os.environ["BACKUP_DIR"], name), "wb").write(b"PGDMP")
            self.assertEqual(api(self.root).post(f"/api/v1/core/backups/{name}/restore/",
                                                 {"confirm": f"RESTORE {name}"}, format="json").status_code, 403)
