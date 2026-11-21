"""Retention and disposition: nothing is destroyed without approval; held and checked-out documents are kept;
rejected documents are not proposed again; never across organisations."""
import datetime as dt
import tempfile
from pathlib import Path

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import DispositionBatch, DocumentCheckout, File, LegalHold, Matter, Run
from core.retention import retention_review
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from document_operations.models import FileFolderLink
from document_operations.utils import get_or_create_folder_tree


class RetentionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Retention firm")
        cls.other = Client.objects.create(name="Other retention firm")
        cls.compliance = make_user("compliance@ret.test", cls.firm, groups=["ComplianceOfficer"])
        cls.lawyer = make_user("lawyer@ret.test", cls.firm, groups=["Client"])
        cls.outsider = make_user("outsider@otherret.test", cls.other, groups=["Admin"])
        cls.matter = Matter.objects.create(client=cls.firm, name="Closed matter", created_by=cls.lawyer)

    def setUp(self):
        cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = get_or_create_folder_tree(["Old"], user=self.lawyer, project_id=self.matter.name, service_id="matter")
        self.old = self.doc("old.pdf", days=400)
        self.held = self.doc("held.pdf", days=400)
        self.locked = self.doc("locked.pdf", days=400)
        self.recent = self.doc("recent.pdf", days=10)
        self.theirs = self.doc("theirs.pdf", days=400, owner=self.outsider, project="Closed matter")

    def doc(self, name, days, owner=None, project=None):
        owner = owner or self.lawyer
        path = Path(self.tmp.name) / f"{owner.pk}-{name}"
        path.write_bytes(b"%PDF-1.4 " + name.encode())
        f = File.objects.create(user=owner, run=Run.objects.create(user=owner), filename=name, filepath=str(path),
                                project_id=project or self.matter.name, service_id="matter", md5_hash=f"{owner.pk}{name}")
        File.objects.filter(pk=f.pk).update(created_at=timezone.now() - dt.timedelta(days=days))
        if owner == self.lawyer:
            FileFolderLink.objects.create(file=f, folder=self.folder)
        return File.objects.get(pk=f.pk)

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def schedule(self, **extra):
        r = self.api(self.compliance).post("/api/v1/core/retention/schedules/", {
            "name": "Closed matters: 1 year", "retain_days": 365, "matter_id": str(self.matter.id), **extra}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["id"]

    def test_review_proposes_but_never_deletes(self):
        self.schedule()
        retention_review()
        batch = DispositionBatch.objects.get()
        self.assertEqual(batch.status, "pending")
        names = sorted(batch.items.values_list("filename", flat=True))
        self.assertEqual(names, ["held.pdf", "locked.pdf", "old.pdf"])          # recent and other organisation excluded
        self.assertEqual(File.objects.count(), 5)                              # nothing destroyed yet
        retention_review()
        self.assertEqual(DispositionBatch.objects.count(), 1)                  # one pending batch at a time

    def test_approval_deletes_but_keeps_held_and_checked_out_documents(self):
        sid = self.schedule()
        r = self.api(self.compliance).post(f"/api/v1/core/retention/schedules/{sid}/review/")
        batch_id = r.json()["batch"]["id"]
        hold = LegalHold.objects.create(client=self.firm, name="Late claim", created_by=self.compliance)
        hold.files.add(self.held)
        DocumentCheckout.objects.create(file=self.locked, user=self.lawyer)
        old_path = Path(self.old.filepath)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.api(self.compliance).post(f"/api/v1/core/retention/batches/{batch_id}/approve/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["outcomes"], {"deleted": 1, "held": 1, "checked_out": 1})
        self.assertFalse(File.objects.filter(pk=self.old.pk).exists())
        self.assertFalse(old_path.exists())
        self.assertTrue(File.objects.filter(pk__in=[self.held.pk, self.locked.pk, self.recent.pk, self.theirs.pk]).count() == 4)
        outcomes = {i["filename"]: i["outcome"] for i in r.json()["items"]}
        self.assertEqual(outcomes, {"old.pdf": "deleted", "held.pdf": "held", "locked.pdf": "checked_out"})

    def test_rejected_documents_are_kept_and_not_proposed_again(self):
        sid = self.schedule()
        batch_id = self.api(self.compliance).post(f"/api/v1/core/retention/schedules/{sid}/review/").json()["batch"]["id"]
        r = self.api(self.compliance).post(f"/api/v1/core/retention/batches/{batch_id}/reject/", {"note": "Keep"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(File.objects.count(), 5)
        r = self.api(self.compliance).post(f"/api/v1/core/retention/schedules/{sid}/review/")
        self.assertIsNone(r.json()["batch"])
        self.assertEqual(self.api(self.compliance).post(f"/api/v1/core/retention/batches/{batch_id}/approve/").status_code, 400)

    def test_permissions_and_isolation(self):
        body = {"name": "x", "retain_days": 30}
        self.assertEqual(self.api(self.lawyer).post("/api/v1/core/retention/schedules/", body, format="json").status_code, 403)
        self.assertEqual(self.api(self.lawyer).get("/api/v1/core/retention/batches/").status_code, 403)
        sid = self.schedule()
        batch_id = self.api(self.compliance).post(f"/api/v1/core/retention/schedules/{sid}/review/").json()["batch"]["id"]
        outsider = self.api(self.outsider)
        self.assertEqual(outsider.get("/api/v1/core/retention/batches/").json()["results"], [])
        self.assertEqual(outsider.post(f"/api/v1/core/retention/batches/{batch_id}/approve/").status_code, 404)
        self.assertEqual(outsider.post(f"/api/v1/core/retention/schedules/{sid}/review/").status_code, 404)
        r = outsider.post("/api/v1/core/retention/schedules/", {**body, "matter_id": str(self.matter.id)}, format="json")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(File.objects.count(), 5)

    def test_validation(self):
        api = self.api(self.compliance)
        self.assertEqual(api.post("/api/v1/core/retention/schedules/", {"name": "x", "retain_days": 0}, format="json").status_code, 400)
        self.assertEqual(api.post("/api/v1/core/retention/schedules/", {"retain_days": 30}, format="json").status_code, 400)
