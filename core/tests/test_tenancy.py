"""
Multi-tenancy and RBAC guarantees for the API layer.

Two organisations (Firm A, Firm B) with ordinary users, an organisation admin and
a platform superuser. Every test states one rule the platform must keep.
Run: python manage.py test core.tests
"""
import os
import tempfile
import zipfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.http import Http404
from django.test import TestCase, override_settings
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application
from rest_framework.test import APIClient

from core import access
from core.models import BulkJob, BulkJobFileResult, File, Run
from custom_authentication.models import Client

User = get_user_model()


def make_user(email, client, *, staff=False, superuser=False, groups=()):
    u = User.objects.create_user(email=email, password="pw-Test-12345", client=client)
    u.username = email.split("@")[0]
    u.is_staff = staff
    u.is_superuser = superuser
    u.save()
    for g in groups:
        u.groups.add(Group.objects.get_or_create(name=g)[0])
    return u


def make_file(owner, name="doc.pdf", path=None):
    run = Run.objects.create(user=owner, status="Completed")
    return File.objects.create(
        filename=name, filepath=path or f"/tmp/{owner.id}-{name}", user=owner, run=run,
        project_id="p1", service_id="s1", md5_hash=f"{owner.id}-{name}",
    )


def token_for(user):
    app = Application.objects.create(
        user=user, client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_PASSWORD, name=f"app-{user.id}",
    )
    tok = AccessToken.objects.create(
        user=user, application=app, token=f"tok-{user.id}",
        expires=timezone.now() + timedelta(hours=1), scope="read write",
    )
    return app, tok


class TenancyPolicyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm_a = Client.objects.create(name="Firm A")
        cls.firm_b = Client.objects.create(name="Firm B")
        cls.a1 = make_user("a1@firm-a.test", cls.firm_a, groups=["Client"])
        cls.a2 = make_user("a2@firm-a.test", cls.firm_a, groups=["Client"])
        cls.a_admin = make_user("admin@firm-a.test", cls.firm_a, staff=True, groups=["Admin"])
        cls.b1 = make_user("b1@firm-b.test", cls.firm_b, groups=["Client"])
        cls.root = make_user("root@platform.test", None, staff=True, superuser=True)
        cls.lonely = make_user("lonely@nowhere.test", None, groups=["Client"])
        cls.fa1 = make_file(cls.a1, "a1-contract.pdf")
        cls.fb1 = make_file(cls.b1, "b1-secret.pdf")
        cls.f_lonely = make_file(cls.lonely, "lonely.pdf")

    # -- files ---------------------------------------------------------------
    def test_owner_can_access_own_file(self):
        self.assertTrue(access.can_access_file(self.a1, self.fa1))

    def test_other_tenant_cannot_access_file(self):
        self.assertFalse(access.can_access_file(self.a1, self.fb1))
        self.assertFalse(access.can_access_file(self.b1, self.fa1))

    def test_colleague_needs_an_explicit_share(self):
        self.assertFalse(access.can_access_file(self.a2, self.fa1))
        from document_operations.models import EffectiveAccess
        EffectiveAccess.objects.create(user=self.a2, file=self.fa1, can_download=True)
        self.assertTrue(access.can_access_file(self.a2, self.fa1))
        self.assertFalse(access.can_access_file(self.a2, self.fa1, "delete"))

    def test_tenant_admin_sees_own_tenant_only(self):
        self.assertTrue(access.can_access_file(self.a_admin, self.fa1))
        self.assertFalse(access.can_access_file(self.a_admin, self.fb1))

    def test_platform_superuser_sees_everything(self):
        self.assertTrue(access.can_access_file(self.root, self.fb1))

    def test_users_without_organisation_are_isolated(self):
        self.assertFalse(access.can_access_file(self.lonely, self.fa1))
        self.assertTrue(access.can_access_file(self.lonely, self.f_lonely))
        self.assertNotIn(self.f_lonely, access.accessible_files(self.a_admin))

    def test_lookup_of_foreign_file_is_404_not_403(self):
        with self.assertRaises(Http404):
            access.get_file_for_user(self.a1, self.fb1.id)

    def test_bulk_selection_rejects_any_foreign_file(self):
        with self.assertRaises(Http404):
            access.get_files_for_user(self.a1, [self.fa1.id, self.fb1.id])
        self.assertEqual(access.get_files_for_user(self.a1, [self.fa1.id]), [self.fa1])

    def test_accessible_files_queryset(self):
        ids = set(access.accessible_files(self.a1).values_list("id", flat=True))
        self.assertEqual(ids, {self.fa1.id})

    # -- processing runs -----------------------------------------------------
    def test_runs_are_scoped_to_creator_and_tenant_admin(self):
        from document_translation.models import TranslationRun
        run_b = TranslationRun.objects.create(
            project_id="p", service_id="s", status="Pending", **access.run_owner_fields(self.b1)
        )
        with self.assertRaises(Http404):
            access.get_run_for_user(self.a1, TranslationRun, run_b.id)
        with self.assertRaises(Http404):
            access.get_run_for_user(self.a_admin, TranslationRun, run_b.id)
        self.assertEqual(access.get_run_for_user(self.b1, TranslationRun, run_b.id), run_b)

    # -- user management -----------------------------------------------------
    def test_admin_manages_only_own_tenant(self):
        self.assertTrue(access.can_manage_user(self.a_admin, self.a1))
        self.assertFalse(access.can_manage_user(self.a_admin, self.b1))
        self.assertFalse(access.can_manage_user(self.a_admin, self.root))
        self.assertFalse(access.can_manage_user(self.a1, self.a2))

    def test_scope_users(self):
        visible = set(access.scope_users(User.objects.all(), self.a_admin))
        self.assertEqual(visible, {self.a1, self.a2, self.a_admin})

    # -- roles ---------------------------------------------------------------
    def test_roles_come_from_the_user(self):
        self.assertIn("Client", access.user_role_names(self.a1))
        self.assertTrue(access.is_tenant_admin(self.a_admin))
        self.assertFalse(access.is_tenant_admin(self.a1))
        self.assertTrue(access.user_has_perm(self.a1, "ocr.submit"))
        self.assertFalse(access.user_has_perm(self.a1, "user.delete"))


@override_settings(ROOT_URLCONF="aiDocuMines.urls")
class HttpEnforcementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm_a = Client.objects.create(name="Firm A")
        cls.firm_b = Client.objects.create(name="Firm B")
        cls.a1 = make_user("a1@firm-a.test", cls.firm_a, groups=["Client"])
        cls.a_admin = make_user("admin@firm-a.test", cls.firm_a, staff=True, groups=["Admin"])
        cls.b1 = make_user("b1@firm-b.test", cls.firm_b, groups=["Client"])
        cls.app_a1, cls.tok_a1 = token_for(cls.a1)
        cls.app_admin, cls.tok_admin = token_for(cls.a_admin)
        cls.app_b1, cls.tok_b1 = token_for(cls.b1)

    def client_for(self, tok, **headers):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {tok.token}", **headers)
        return c

    def test_whoami_reports_verified_identity(self):
        r = self.client_for(self.tok_a1).get("/api/v1/auth/whoami/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["client_org_id"], self.firm_a.id)
        self.assertFalse(r.json()["is_tenant_admin"])

    def test_foreign_client_id_header_is_rejected(self):
        r = self.client_for(self.tok_a1, HTTP_X_CLIENT_ID=self.app_b1.client_id).get("/api/v1/auth/whoami/")
        self.assertEqual(r.status_code, 403)

    def test_own_client_id_header_is_accepted(self):
        r = self.client_for(self.tok_a1, HTTP_X_CLIENT_ID=self.app_a1.client_id).get("/api/v1/auth/whoami/")
        self.assertEqual(r.status_code, 200)

    def test_x_role_header_does_not_grant_admin(self):
        r = self.client_for(self.tok_a1, HTTP_X_ROLE="Admin").get("/api/v1/auth/admin/users/")
        self.assertEqual(r.status_code, 403)

    def test_admin_user_list_is_tenant_scoped(self):
        r = self.client_for(self.tok_admin).get("/api/v1/auth/admin/users/")
        self.assertEqual(r.status_code, 200)
        emails = {u["email"] for u in r.json()}
        self.assertIn("a1@firm-a.test", emails)
        self.assertNotIn("b1@firm-b.test", emails)

    def test_admin_cannot_disable_user_of_other_tenant(self):
        r = self.client_for(self.tok_admin).post(f"/api/v1/auth/admin/users/{self.b1.id}/disable/")
        self.assertIn(r.status_code, (403, 404))
        self.b1.refresh_from_db()
        self.assertTrue(self.b1.is_active)

    def test_org_admin_promotion_does_not_create_platform_superuser(self):
        r = self.client_for(self.tok_admin).post(f"/api/v1/auth/admin/users/{self.a1.id}/promote/", {"platform_superuser": "true"})
        self.a1.refresh_from_db()
        self.assertFalse(self.a1.is_superuser)

    def test_admin_initialize_never_returns_passwords(self):
        User.objects.create_superuser(email="root@platform.test", password="x", client=None)
        r = APIClient().post("/api/v1/auth/admin/initialize/", {})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("password", r.content.decode().lower())

    def test_break_glass_endpoints_disabled_by_default(self):
        r = APIClient().post("/api/v1/auth/admin/supersuperuser-clean-db/", {"email": "x", "password": "y"})
        self.assertEqual(r.status_code, 404)

    def test_onlyoffice_callback_rejects_unsigned_foreign_url(self):
        r = APIClient().post("/api/v1/core/onlyoffice/callback/?file_id=1",
                             {"status": 2, "url": "http://attacker.example/evil.docx"}, format="json")
        self.assertEqual(r.status_code, 403)


class BulkDeliverablesTests(TestCase):
    def test_workbook_and_zip_contain_real_outputs(self):
        firm = Client.objects.create(name="Firm A")
        user = make_user("u@firm-a.test", firm, groups=["Client"])
        tmp = tempfile.mkdtemp()
        src_path = os.path.join(tmp, "contract.pdf")
        out_path = os.path.join(tmp, "contract_translated_fr.docx")
        for p, data in ((src_path, b"%PDF-src"), (out_path, b"translated")):
            with open(p, "wb") as fh:
                fh.write(data)
        src = make_file(user, "contract.pdf", src_path)
        out = make_file(user, "contract_translated_fr.docx", out_path)

        job = BulkJob.objects.create(user=user, job_type="ai_translation", status="Completed",
                                     total_files=1, project_id="p1", service_id="s1",
                                     completed_at=timezone.now())
        BulkJobFileResult.objects.create(
            bulk_job=job, file=src, status="Completed",
            result_data={"outputs": [{"file_id": out.id, "filename": out.filename}],
                         "fields": {"Target language": "fr", "Characters": 1200}},
        )

        from core.bulk_outputs import build_deliverables
        d = build_deliverables(job, "Translation")

        xlsx = File.objects.get(id=d["xlsx_file_id"])
        self.assertTrue(xlsx.filename.endswith("results.xlsx"))
        from openpyxl import load_workbook
        ws = load_workbook(xlsx.filepath)["Results"]
        header = [c.value for c in ws[1]]
        self.assertEqual(header[:3], ["Document", "Status", "Output documents"])
        self.assertIn("Target language", header)
        self.assertEqual(ws.cell(row=2, column=1).value, "contract.pdf")
        self.assertIn("contract_translated_fr.docx", ws.cell(row=2, column=3).value)

        z = File.objects.get(id=d["zip_file_id"])
        with zipfile.ZipFile(z.filepath) as zf:
            names = zf.namelist()
        self.assertIn("contract/contract_translated_fr.docx", names)
        self.assertIn("results.xlsx", names)

    def test_summarise_value_flattens_service_results(self):
        from core.bulk_outputs import summarise_value
        out = summarise_value({"task_id": "x", "document_type": "NDA", "risks": ["a", "b"],
                               "scores": {"overall": 0.8}, "items": [{"a": 1}]})
        self.assertEqual(out["Document type"], "NDA")
        self.assertEqual(out["Risks"], "a; b")
        self.assertEqual(out["Scores › Overall"], 0.8)
        self.assertEqual(out["Items (count)"], 1)
        self.assertNotIn("Task id", out)
