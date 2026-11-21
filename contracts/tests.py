"""Contract lifecycle and obligations: stages, approvals with separation of duties, automatic obligations,
renewals and reminders, all within one organisation and respecting ethical walls."""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import EthicalWall, File, Matter, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from .lifecycle import run_daily
from .models import ApprovalRule, Contract, ContractEvent, Obligation


class ContractTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="CLM firm")
        cls.other = Client.objects.create(name="Other CLM firm")
        cls.legal = make_user("legal@clm.test", cls.firm, groups=["Manager"])          # contract manager
        cls.cfo = make_user("cfo@clm.test", cls.firm, groups=["Executive"])            # sees all, read only
        cls.sales = make_user("sales@clm.test", cls.firm, groups=["Client"])          # requests contracts
        cls.colleague = make_user("colleague@clm.test", cls.firm, groups=["Client"])
        cls.approver = make_user("approver@clm.test", cls.firm, groups=["Client"])
        cls.outsider = make_user("outsider@otherclm.test", cls.other, groups=["Admin"])
        cls.guest = make_user("guest@clm.test", cls.firm, groups=["Guest"])

    def setUp(self):
        cache.clear()

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def signed_doc(self, owner):
        return File.objects.create(user=owner, run=Run.objects.create(user=owner), filename="signed.pdf",
                                   filepath="/nonexistent/signed.pdf", project_id="Deals", service_id="matter")

    def request_contract(self, **extra):
        r = self.api(self.sales).post("/api/v1/contracts/intake/", {"title": "Reseller agreement", "counterparty": "Acme",
                                      "contract_type": "MSA", "value": "250000", "currency": "zar", **extra}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["id"]

    def move(self, user, cid, to, code=200):
        r = self.api(user).post(f"/api/v1/contracts/contracts/{cid}/advance/", {"to": to}, format="json")
        self.assertEqual(r.status_code, code, r.content)
        return r.json()

    def test_intake_to_active_with_approvals_and_separation_of_duties(self):
        ApprovalRule.objects.create(client=self.firm, name="Large deals", min_value=Decimal("100000"), approver=self.approver)
        ApprovalRule.objects.create(client=self.firm, name="Small NDAs only", contract_type="NDA", approver=self.colleague)
        cid = self.request_contract()
        # The requester cannot move it; legal picks it up and becomes the owner.
        self.move(self.sales, cid, "draft", 403)
        data = self.move(self.legal, cid, "draft")
        self.assertEqual(data["owner"]["email"], "legal@clm.test")
        self.move(self.legal, cid, "review")
        data = self.move(self.legal, cid, "approval")
        self.assertEqual(len(data["approvals"]), 1)                       # only the rule that applies
        approval_id = data["approvals"][0]["id"]
        # Nobody but the named approver decides; the owner cannot approve their own contract.
        self.assertEqual(self.api(self.legal).post(f"/api/v1/contracts/approvals/{approval_id}/decide/",
                                                   {"decision": "approve"}, format="json").status_code, 403)
        self.assertEqual(self.api(self.colleague).post(f"/api/v1/contracts/approvals/{approval_id}/decide/",
                                                       {"decision": "approve"}, format="json").status_code, 404)
        mine = self.api(self.approver).get("/api/v1/contracts/approvals/mine/").json()["results"]
        self.assertEqual([a["id"] for a in mine], [approval_id])
        r = self.api(self.approver).post(f"/api/v1/contracts/approvals/{approval_id}/decide/", {"decision": "approve"}, format="json")
        self.assertEqual(r.json()["stage"], "signature")
        # Active needs the signed document and a start date.
        self.move(self.legal, cid, "active", 400)
        doc = self.signed_doc(self.legal)
        today = date.today()
        self.api(self.legal).patch(f"/api/v1/contracts/contracts/{cid}/", {"document_id": doc.pk, "start_date": str(today),
                                   "end_date": str(today + timedelta(days=365)), "notice_days": 60}, format="json")
        data = self.move(self.legal, cid, "active")
        kinds = sorted(o["kind"] for o in data["obligations"])
        self.assertEqual(kinds, ["expiry", "renewal"])
        renewal = next(o for o in data["obligations"] if o["kind"] == "renewal")
        self.assertEqual(renewal["due_date"], str(today + timedelta(days=305)))
        # Every step is in the append-only history.
        self.assertGreaterEqual(ContractEvent.objects.filter(contract_id=cid, kind="stage").count(), 5)

    def test_rejection_returns_to_review_and_no_rule_skips_approval(self):
        ApprovalRule.objects.create(client=self.firm, name="All", approver=self.approver)
        cid = self.request_contract()
        for to in ("draft", "review", "approval"):
            data = self.move(self.legal, cid, to)
        r = self.api(self.approver).post(f"/api/v1/contracts/approvals/{data['approvals'][0]['id']}/decide/",
                                         {"decision": "reject", "note": "Liability cap too high"}, format="json")
        self.assertEqual(r.json()["stage"], "review")
        ApprovalRule.objects.all().update(active=False)
        self.assertEqual(self.move(self.legal, cid, "approval")["stage"], "signature")

    def test_illegal_moves_are_refused(self):
        cid = self.request_contract()
        self.move(self.legal, cid, "active", 400)
        self.move(self.legal, cid, "terminated")
        self.move(self.legal, cid, "draft", 400)
        self.assertEqual(self.api(self.legal).patch(f"/api/v1/contracts/contracts/{cid}/", {"title": "x"}, format="json").status_code, 400)

    def test_visibility_and_isolation(self):
        cid = self.request_contract()
        self.assertEqual(self.api(self.colleague).get(f"/api/v1/contracts/contracts/{cid}/").status_code, 404)
        self.assertEqual(self.api(self.outsider).get(f"/api/v1/contracts/contracts/{cid}/").status_code, 404)
        self.assertEqual(self.api(self.outsider).get("/api/v1/contracts/contracts/").json()["results"], [])
        cfo = self.api(self.cfo)
        self.assertEqual(cfo.get(f"/api/v1/contracts/contracts/{cid}/").status_code, 200)       # sees all, read only
        self.assertEqual(cfo.patch(f"/api/v1/contracts/contracts/{cid}/", {"title": "x"}, format="json").status_code, 403)
        self.assertEqual(self.api(self.guest).post("/api/v1/contracts/intake/", {"title": "x"}, format="json").status_code, 403)
        # Another organisation's document or person can never be attached.
        foreign_doc = self.signed_doc(self.outsider)
        r = self.api(self.legal).patch(f"/api/v1/contracts/contracts/{cid}/", {"document_id": foreign_doc.pk}, format="json")
        self.assertEqual(r.status_code, 404)
        r = self.api(self.legal).patch(f"/api/v1/contracts/contracts/{cid}/", {"owner_id": self.outsider.pk}, format="json")
        self.assertEqual(r.status_code, 404)

    def test_ethical_wall_hides_the_matters_contracts(self):
        matter = Matter.objects.create(client=self.firm, name="Falcon", created_by=self.legal)
        c = Contract.objects.create(client=self.firm, title="Falcon SPA", matter=matter, stage="draft", owner=self.legal)
        self.assertEqual(self.api(self.cfo).get(f"/api/v1/contracts/contracts/{c.pk}/").status_code, 200)
        wall = EthicalWall.objects.create(client=self.firm, matter=matter, created_by=self.legal)
        wall.excluded.add(self.cfo)
        cfo = type(self.cfo).objects.get(pk=self.cfo.pk)
        self.assertEqual(self.api(cfo).get(f"/api/v1/contracts/contracts/{c.pk}/").status_code, 404)

    def test_renewal_window_expiry_and_auto_renewal(self):
        today = date.today()
        ending = Contract.objects.create(client=self.firm, title="Lease", stage="active", owner=self.legal,
                                         start_date=today - timedelta(days=300), end_date=today + timedelta(days=20), notice_days=30)
        lapsed = Contract.objects.create(client=self.firm, title="Old NDA", stage="active", owner=self.legal,
                                         start_date=today - timedelta(days=400), end_date=today - timedelta(days=1))
        rolling = Contract.objects.create(client=self.firm, title="SaaS", stage="active", owner=self.legal, auto_renew=True,
                                          renewal_term_months=12, start_date=today - timedelta(days=400), end_date=today - timedelta(days=2))
        with mock.patch("email_management.tasks.send_outbox_email.delay"):
            moved = run_daily(today)
        self.assertEqual((moved["renewal"], moved["expired"], moved["renewed"]), (1, 1, 1))
        for c, stage in ((ending, "renewal"), (lapsed, "expired"), (rolling, "active")):
            c.refresh_from_db()
            self.assertEqual(c.stage, stage)
        self.assertGreater(rolling.end_date, today)

    def test_obligations_reminders_recurrence_and_scope(self):
        api = self.api(self.sales)
        r = api.post("/api/v1/contracts/obligations/", {"title": "Quarterly report", "due_date": str(date.today() + timedelta(days=5)),
                     "recurrence": "quarterly", "reminder_days": 7}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        oid = r.json()["id"]
        self.assertEqual(self.api(self.colleague).patch(f"/api/v1/contracts/obligations/{oid}/", {"action": "done"},
                                                        format="json").status_code, 404)
        with mock.patch("email_management.tasks.send_outbox_email.delay") as send:
            self.assertEqual(run_daily()["reminders"], 1)
            self.assertEqual(run_daily()["reminders"], 0)          # once a day
        send.assert_called_once()
        r = api.patch(f"/api/v1/contracts/obligations/{oid}/", {"action": "done"}, format="json")
        self.assertEqual(r.json()["status"], "done")
        self.assertIsNotNone(r.json()["next"])                     # the next quarter's occurrence
        self.assertEqual(self.api(self.outsider).get("/api/v1/contracts/obligations/").json()["results"], [])

    def test_validation(self):
        api = self.api(self.sales)
        for bad in ({"title": ""}, {"title": "x", "value": "-5"}, {"title": "x", "currency": "RANDS"},
                    {"title": "x", "start_date": "2026-05-01", "end_date": "2026-01-01"}, {"title": "x", "needed_by": "soon"}):
            self.assertEqual(api.post("/api/v1/contracts/intake/", bad, format="json").status_code, 400, bad)


class ImportPEObligationsTests(TestCase):
    def test_copies_private_equity_obligations_once_and_keeps_the_originals(self):
        from io import StringIO
        from django.core.management import call_command
        from private_equity.models import PostCloseObligation
        firm = Client.objects.create(name="PE firm")
        other = Client.objects.create(name="Other PE firm")
        owner = make_user("pe@pe.test", firm, groups=["Client"])
        stranger = make_user("x@otherpe.test", other, groups=["Client"])
        from private_equity.models import DueDiligenceRun
        choice = lambda f: DueDiligenceRun._meta.get_field(f).choices[0][0]
        run = DueDiligenceRun.objects.create(client=firm, run=Run.objects.create(user=owner), deal_name="Project Kite",
                                             target_company="Kite Ltd", deal_type=choice("deal_type"),
                                             data_room_source=choice("data_room_source"))
        PostCloseObligation.objects.create(client=firm, due_diligence_run=run, obligation_name="Annual audited accounts",
                                           obligation_type=PostCloseObligation._meta.get_field("obligation_type").choices[0][0],
                                           frequency="annually", due_date=date(2027, 3, 31), assigned_to=stranger)
        call_command("import_pe_obligations", stdout=StringIO())
        call_command("import_pe_obligations", stdout=StringIO())        # idempotent
        o = Obligation.objects.get(client=firm)
        self.assertEqual((o.title, o.recurrence, o.due_date), ("Annual audited accounts", "yearly", date(2027, 3, 31)))
        self.assertIsNone(o.owner)                                          # never an owner from another organisation
        self.assertEqual(PostCloseObligation.objects.count(), 1)
