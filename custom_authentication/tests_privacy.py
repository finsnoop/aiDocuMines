"""Data-subject rights for platform users (POPIA/GDPR): export, erasure and retention defaults.
Erasure removes personal data and sign-in secrets, keeps the organisation's documents (or transfers them), never
crosses organisations, respects legal holds, and leaves the audit trail intact."""
import json
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application
from rest_framework.test import APIClient

from core.models import File, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import (AdminAuditEvent, APIKey, Client, CustomUser, TwoFactorCode,
                                          UserActivityLog)


class PrivacyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Privacy firm")
        cls.other = Client.objects.create(name="Other privacy firm")
        cls.admin = make_user("admin@privacy.test", cls.firm, groups=["Admin"])
        cls.person = make_user("leaver@privacy.test", cls.firm, groups=["Client"])
        cls.colleague = make_user("colleague@privacy.test", cls.firm, groups=["Client"])
        cls.outsider_admin = make_user("otheradmin@otherprivacy.test", cls.other, groups=["Admin"])

    def setUp(self):
        p = self.person
        p.first_name, p.last_name, p.contact_phone, p.address = "Lerato", "Mokoena", "+27 82 000 0000", "1 Main Rd"
        p.totp_secret, p.is_2fa_enabled = "JBSWY3DPEHPK3PXP", True
        p.save()
        self.doc = File.objects.create(user=p, run=Run.objects.create(user=p), filename="matter-note.docx",
                                       filepath="/nonexistent/matter-note.docx", project_id="P", service_id="matter",
                                       md5_hash="note")
        UserActivityLog.objects.create(user=p, event="CHAT_QUERY", metadata={"query_preview": "salary of Lerato"})
        UserActivityLog.objects.create(user=p, event="LOGIN", metadata={"ip": "10.0.0.7"})
        APIKey.objects.create(user=p, client_id="key-leaver", client_secret_hashed="x")
        TwoFactorCode.objects.create(user=p, code="123456")
        app = Application.objects.create(name="spa", client_type="public", authorization_grant_type="authorization-code")
        AccessToken.objects.create(user=p, token="tok-leaver", application=app, expires=timezone.now() + timedelta(hours=1),
                                   scope="read write")

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    # ── export ──
    def test_people_can_export_their_own_data_without_secrets(self):
        r = self.api(self.person).get("/api/v1/auth/me/personal-data/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r["Content-Disposition"])
        data = json.loads(r.content)
        self.assertEqual(data["profile"]["email"], "leaver@privacy.test")
        self.assertEqual(data["profile"]["last_name"], "Mokoena")
        self.assertEqual(data["organisation"], "Privacy firm")
        self.assertEqual(data["documents_owned"][0]["filename"], "matter-note.docx")
        self.assertEqual({a["event"] for a in data["activity"]}, {"CHAT_QUERY", "LOGIN"})
        body = r.content.decode()
        for secret in ("JBSWY3DPEHPK3PXP", "tok-leaver", "client_secret", "password"):
            self.assertNotIn(secret, body)

    def test_administrators_export_people_in_their_own_organisation_only(self):
        self.assertEqual(self.api(self.admin).get(f"/api/v1/auth/admin/people/{self.person.pk}/personal-data/").status_code, 200)
        self.assertEqual(self.api(self.outsider_admin).get(f"/api/v1/auth/admin/people/{self.person.pk}/personal-data/").status_code, 404)
        self.assertEqual(self.api(self.colleague).get(f"/api/v1/auth/admin/people/{self.person.pk}/personal-data/").status_code, 403)
        self.assertTrue(AdminAuditEvent.objects.filter(action="privacy.export", target_id=str(self.person.pk)).exists())

    # ── erasure ──
    def erase(self, user=None, **body):
        body.setdefault("confirm_email", "leaver@privacy.test")
        return self.api(user or self.admin).post(f"/api/v1/auth/admin/people/{self.person.pk}/erase/", body, format="json")

    def test_erasure_removes_personal_data_and_secrets_and_keeps_documents(self):
        r = self.erase()
        self.assertEqual(r.status_code, 200, r.content)
        p = CustomUser.objects.get(pk=self.person.pk)
        self.assertEqual(p.email, f"erased-{p.pk}@erased.invalid")
        self.assertFalse(p.is_active)
        self.assertFalse(p.has_usable_password())
        self.assertEqual((p.first_name, p.last_name), ("", ""))
        self.assertFalse(p.contact_phone)
        self.assertFalse(p.address)
        self.assertIsNone(p.totp_secret)
        self.assertFalse(p.groups.exists())
        self.assertFalse(AccessToken.objects.filter(user=p).exists())
        self.assertFalse(APIKey.objects.filter(user=p).exists())
        self.assertFalse(TwoFactorCode.objects.filter(user=p).exists())
        self.assertFalse(UserActivityLog.objects.filter(user=p, event="CHAT_QUERY").exists())
        self.assertIsNone(UserActivityLog.objects.get(user=p, event="LOGIN").metadata)
        self.assertTrue(File.objects.filter(pk=self.doc.pk, user=p).exists())          # the organisation's record stays
        event = AdminAuditEvent.objects.get(action="privacy.erase")
        self.assertNotIn("leaver@privacy.test", json.dumps([event.target_label, event.details]))
        # Erasing twice is refused clearly.
        self.assertEqual(self.api(self.admin).post(f"/api/v1/auth/admin/people/{p.pk}/erase/",
                                                   {"confirm_email": p.email}, format="json").status_code, 409)

    def test_documents_can_be_transferred_to_a_colleague_but_held_ones_stay(self):
        held = File.objects.create(user=self.person, run=Run.objects.create(user=self.person), filename="held.pdf",
                                   filepath="/nonexistent/held.pdf", project_id="P", service_id="matter", md5_hash="held")
        with mock.patch("core.holds.is_held", side_effect=lambda f: f.pk == held.pk):
            r = self.erase(transfer_to="colleague@privacy.test")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["summary"]["documents_transferred"], 1)
        self.assertEqual(File.objects.get(pk=self.doc.pk).user, self.colleague)
        self.assertEqual(File.objects.get(pk=held.pk).user_id, self.person.pk)

    def test_erasure_is_confirmed_scoped_and_guarded(self):
        self.assertEqual(self.erase(confirm_email="wrong@privacy.test").status_code, 400)
        self.assertEqual(self.erase(user=self.outsider_admin).status_code, 404)        # another organisation
        self.assertEqual(self.erase(user=self.colleague).status_code, 403)             # not an administrator
        outsider = make_user("someone@otherprivacy.test", self.other, groups=["Client"])
        self.assertEqual(self.erase(transfer_to=outsider.pk).status_code, 409)         # documents never leave the organisation
        r = self.api(self.admin).post(f"/api/v1/auth/admin/people/{self.admin.pk}/erase/",
                                      {"confirm_email": self.admin.email}, format="json")
        self.assertEqual(r.status_code, 409)                                           # not yourself
        self.assertTrue(CustomUser.objects.get(pk=self.person.pk).is_active)            # nothing happened

    def test_last_administrator_cannot_be_erased(self):
        second = make_user("admin2@privacy.test", self.firm, groups=["Admin"])
        r = self.api(second).post(f"/api/v1/auth/admin/people/{self.admin.pk}/erase/", {"confirm_email": self.admin.email}, format="json")
        self.assertEqual(r.status_code, 200, r.content)                                # another admin remains
        r = self.api(self.admin).post(f"/api/v1/auth/admin/people/{second.pk}/erase/", {"confirm_email": second.email}, format="json")
        self.assertIn(r.status_code, (403, 404))                                        # the erased admin can do nothing

    def test_hard_delete_refuses_people_who_own_documents(self):
        root = make_user("root@privacy.test", self.firm, superuser=True)
        with mock.patch("oauth2_provider.contrib.rest_framework.TokenHasReadWriteScope.has_permission", return_value=True):
            r = self.api(root).delete(f"/api/v1/auth/admin/users/{self.person.pk}/delete/")
        self.assertEqual(r.status_code, 409, r.content)
        self.assertIn("Erase personal data", r.json()["error"])
        self.assertTrue(File.objects.filter(pk=self.doc.pk).exists())
        self.assertTrue(CustomUser.objects.filter(pk=self.person.pk).exists())

    # ── retention ──
    def test_retention_defaults_remove_old_activity_and_questions_only(self):
        from custom_authentication.privacy import apply_retention, retention_days
        old = UserActivityLog.objects.create(user=self.person, event="LOGIN")
        UserActivityLog.objects.filter(pk=old.pk).update(timestamp=timezone.now() - timedelta(days=800))
        out = apply_retention()
        self.assertEqual(out["activity_logs"], 1)
        self.assertEqual(UserActivityLog.objects.filter(user=self.person).count(), 2)  # recent ones kept
        self.assertTrue(File.objects.filter(pk=self.doc.pk).exists())
        with mock.patch.dict("os.environ", {"RETENTION_ACTIVITY_DAYS": "1"}):
            self.assertEqual(retention_days("activity_logs"), 30)                      # floor: never under 30 days

    # ── data residency ──
    def test_residency_statement_reflects_this_organisations_configuration(self):
        from custom_authentication.models import OrganisationPolicy
        OrganisationPolicy.objects.update_or_create(client=self.firm, defaults={
            "ai_provider": "litellm", "default_ai_model": "claude-sonnet-5-5", "assistant_web_enabled": True})
        env = {"DATA_HOSTING_PROVIDER": "Azure", "DATA_HOSTING_REGION": "South Africa North", "EMAIL_HOST": "smtp.test"}
        with mock.patch.dict("os.environ", env):
            r = self.api(self.admin).get("/api/v1/auth/admin/data-residency/")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["storage"]["region"], "South Africa North")
        self.assertEqual(body["storage"]["backups"], "South Africa North")
        self.assertIn("Anthropic", body["ai_processing"]["vendors"])
        self.assertEqual(body["missing"], [])
        with mock.patch.dict("os.environ", {"DATA_HOSTING_PROVIDER": "", "DATA_HOSTING_REGION": "", "DATA_BACKUP_REGION": ""}):
            blank = self.api(self.admin).get("/api/v1/auth/admin/data-residency/").json()
        self.assertEqual(blank["storage"]["provider"], "")
        self.assertEqual(blank["missing"], ["DATA_HOSTING_PROVIDER", "DATA_HOSTING_REGION", "DATA_BACKUP_REGION"])
        self.assertIn("Web search provider", [s["service"] for s in body["other_services"]])
        OrganisationPolicy.objects.filter(client=self.firm).update(ai_provider="ollama")
        local = self.api(self.admin).get("/api/v1/auth/admin/data-residency/").json()["ai_processing"]
        self.assertIn("own servers", local["location"])
        self.assertEqual(self.api(self.person).get("/api/v1/auth/admin/data-residency/").status_code, 403)
        # Another organisation's administrator sees their own organisation, never this one.
        self.assertEqual(self.api(self.outsider_admin).get("/api/v1/auth/admin/data-residency/").json()["organisation"],
                         "Other privacy firm")
