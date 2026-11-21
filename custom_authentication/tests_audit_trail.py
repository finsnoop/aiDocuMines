"""Audit trail: append-only, tamper-evident per organisation, exportable with a checksum, never across organisations."""
import csv
import hashlib
import io
import json

from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import File, LegalHold, Matter, Run
from core.tests.test_tenancy import make_user
from cost_centre.models import EventLog, EventType, ServiceType
from custom_authentication.audit_chain import verify
from custom_authentication.models import AdminAuditEvent, Client


class AuditTrailTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Audit firm")
        cls.other = Client.objects.create(name="Other audit firm")
        cls.admin = make_user("admin@audit.test", cls.firm, groups=["Admin"])
        cls.lawyer = make_user("lawyer@audit.test", cls.firm, groups=["Client"])
        cls.outsider = make_user("outsider@otheraudit.test", cls.other, groups=["Admin"])

    def event(self, client, action):
        return AdminAuditEvent.objects.create(actor=self.admin, client=client, action=action, target_label=action)

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def test_chain_links_each_organisation_and_detects_tampering(self):
        first = self.event(self.firm, "one")
        second = self.event(self.firm, "two")
        self.event(self.other, "elsewhere")                      # a separate chain
        self.assertEqual(second.prev_hash, first.entry_hash)
        self.assertTrue(verify(self.firm.pk)["intact"])
        with connection.cursor() as cur:                         # tamper below the application
            cur.execute(f"UPDATE {AdminAuditEvent._meta.db_table} SET target_label = %s WHERE id = %s", ["edited", first.pk])
        result = verify(self.firm.pk)
        self.assertFalse(result["intact"])
        self.assertEqual(result["broken_at"], first.pk)
        self.assertTrue(verify(self.other.pk)["intact"])

    def test_events_are_append_only(self):
        e = self.event(self.firm, "one")
        e.action = "changed"
        with self.assertRaises(ValueError):
            e.save()
        with self.assertRaises(ValueError):
            e.delete()
        with self.assertRaises(ValueError):
            AdminAuditEvent.objects.filter(pk=e.pk).update(action="x")
        with self.assertRaises(ValueError):
            AdminAuditEvent.objects.filter(pk=e.pk).delete()

    def test_document_deletion_and_governance_actions_are_recorded(self):
        doc = File.objects.create(user=self.lawyer, run=Run.objects.create(user=self.lawyer), filename="gone.pdf",
                                  filepath="/nonexistent/gone.pdf", project_id="M", service_id="matter")
        doc.delete()
        self.assertTrue(AdminAuditEvent.objects.filter(client=self.firm, action="document.deleted", target_label="gone.pdf").exists())
        matter = Matter.objects.create(client=self.firm, name="Audited matter", created_by=self.admin)
        r = self.api(self.admin).post("/api/v1/core/legal-holds/", {"name": "Hold", "matter_id": str(matter.id)}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertTrue(AdminAuditEvent.objects.filter(client=self.firm, action="legal_hold.placed").exists())
        self.assertTrue(verify(self.firm.pk)["intact"])

    def test_export_has_every_kind_with_a_checksum_and_stays_in_the_organisation(self):
        self.event(self.firm, "settings.changed")
        self.event(self.other, "their.secret")
        EventLog.objects.create(user=self.lawyer, tenant=self.firm, event_type=EventType.OTHER, service_type=ServiceType.PAYABLE,
                                tokens_used=42, metadata={"provider": "litellm", "model": "gpt-4o-mini", "task": "chat",
                                                          "key": "platform", "prompt_tokens": 30, "completion_tokens": 12})
        self.assertEqual(self.api(self.lawyer).get("/api/v1/auth/admin/audit/export/").status_code, 403)
        r = self.api(self.admin).get("/api/v1/auth/admin/audit/export/?as=csv")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["X-Content-SHA256"], hashlib.sha256(r.content).hexdigest())
        table = list(csv.DictReader(io.StringIO(r.content.decode())))
        actions = {row["action"] for row in table}
        self.assertIn("settings.changed", actions)
        self.assertIn("ai.call", actions)
        self.assertNotIn("their.secret", actions)
        ai = next(row for row in table if row["action"] == "ai.call")
        self.assertEqual(json.loads(ai["details"])["tokens"], 42)
        # The export itself is recorded.
        self.assertTrue(AdminAuditEvent.objects.filter(client=self.firm, action="audit.exported").exists())
        # JSON Lines works too; another organisation's admin sees only their own.
        r = self.api(self.outsider).get("/api/v1/auth/admin/audit/export/?as=jsonl")
        lines = [json.loads(l) for l in r.content.decode().splitlines()]
        self.assertTrue(lines and all(l["action"] != "settings.changed" for l in lines))

    def test_verify_endpoint(self):
        self.event(self.firm, "one")
        self.assertEqual(self.api(self.lawyer).get("/api/v1/auth/admin/audit/verify/").status_code, 403)
        self.assertTrue(self.api(self.admin).get("/api/v1/auth/admin/audit/verify/").json()["intact"])

    def test_validation(self):
        api = self.api(self.admin)
        self.assertEqual(api.get("/api/v1/auth/admin/audit/export/?start=2020-01-01&end=2026-01-01").status_code, 400)
        self.assertEqual(api.get("/api/v1/auth/admin/audit/export/?as=xml").status_code, 400)
        self.assertEqual(api.get("/api/v1/auth/admin/audit/export/?start=bad").status_code, 400)
