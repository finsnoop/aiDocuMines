"""Playbooks, benchmarks and deviation / compliance analysis (/api/v1/knowledge/).

Never across organisations; roles decide who sees and who changes; documents only through core.access; links by
ID between playbooks, benchmarks, contracts, matters and documents; numbers compared in code (verbatim quote with
the number in it, target, tolerance); the whole module can be switched off per organisation; the Node library
copies across exactly once and never guesses an organisation."""
import io
import tempfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import File, Matter, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from .benchmarking import compare, validate
from .models import (Benchmark, BenchmarkTerm, Contract, KnowledgeLink, Playbook, PlaybookPosition, PlaybookReview)
from .playbook import playbook_review_task

SLA_TEXT = """SERVICE AGREEMENT

4. Availability. The Supplier will make the Service available 99.5% of the time in each calendar month.

6. Support. Critical incidents are answered within 4 hours, at any time.

8. Payment. The Customer pays each invoice within 45 days of receipt.

11. Governing law. This Agreement is governed by the laws of England and Wales."""

MODEL = "home_dash_analytics.intelligence.configured_model"
AGENT = "home_dash_analytics.intelligence.agent_call"


class KnowledgeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Knowledge firm")
        cls.other = Client.objects.create(name="Other knowledge firm")
        cls.manager = make_user("manager@kn.test", cls.firm, groups=["Manager"])
        cls.member = make_user("member@kn.test", cls.firm, groups=["Client"])
        cls.guest = make_user("guest@kn.test", cls.firm, groups=["Guest"])
        cls.outsider = make_user("outsider@otherkn.test", cls.other, groups=["Admin"])

    def setUp(self):
        cache.clear()
        self.matter = Matter.objects.create(client=self.firm, name="Vendor deals", created_by=self.manager)
        self.matter.members.add(self.manager, self.member)
        self.doc = File.objects.create(user=self.manager, run=Run.objects.create(user=self.manager), filename="sla.docx",
                                       filepath="/nonexistent/sla.docx", project_id="Vendor deals", service_id="matter",
                                       content=SLA_TEXT)
        self.policy_doc = File.objects.create(user=self.manager, run=Run.objects.create(user=self.manager),
                                              filename="procurement-policy.pdf", filepath="/nonexistent/policy.pdf",
                                              project_id="Policies", service_id="matter", content="Availability must be at least 99.9%.")
        self.pb = Playbook.objects.create(client=self.firm, name="Supplier SLA", category="legal-contracts",
                                          tags=["sla", "vendors"], created_by=self.manager)
        PlaybookPosition.objects.create(playbook=self.pb, topic="Governing law", standard="Laws of South Africa.", order=1)
        self.bm = Benchmark.objects.create(client=self.firm, name="Procurement policy", kind="policy", file=self.policy_doc)
        self.uptime = BenchmarkTerm.objects.create(benchmark=self.bm, topic="Availability", requirement="At least 99.9% monthly",
                                                   comparator=">=", target_value=Decimal("99.9"), tolerance=Decimal("0.5"),
                                                   unit="%", severity="high", order=1)
        self.payment = BenchmarkTerm.objects.create(benchmark=self.bm, topic="Payment terms", requirement="Pay within 30 days",
                                                    comparator="<=", target_value=Decimal("30"), tolerance=Decimal("5"),
                                                    unit="days", order=2)
        self.response = BenchmarkTerm.objects.create(benchmark=self.bm, topic="Critical response", requirement="Within 2 hours",
                                                     comparator="<=", target_value=Decimal("2"), unit="hours", order=3)
        self.law = BenchmarkTerm.objects.create(benchmark=self.bm, topic="Governing law",
                                                requirement="Governed by South African law", comparator="present", order=4)
        self.escrow = BenchmarkTerm.objects.create(benchmark=self.bm, topic="Source code escrow",
                                                   requirement="Source code escrow is in place", comparator="present", order=5)
        self.contract = Contract.objects.create(client=self.firm, title="Acme SLA", stage="review", owner=self.manager,
                                                document=self.doc, matter=self.matter)

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    # ── tenancy and roles ──
    def test_another_organisation_finds_nothing(self):
        c = self.api(self.outsider)
        for url in (f"/api/v1/knowledge/playbooks/{self.pb.id}/", f"/api/v1/knowledge/benchmarks/{self.bm.id}/",
                    f"/api/v1/knowledge/playbooks/{self.pb.id}/export/", f"/api/v1/knowledge/applicable/?file_id={self.doc.pk}",
                    f"/api/v1/knowledge/links/?matter_id={self.matter.pk}"):
            self.assertEqual(c.get(url).status_code, 404, url)
        self.assertEqual(c.get("/api/v1/knowledge/playbooks/").json()["results"], [])
        self.assertEqual(c.patch(f"/api/v1/knowledge/playbooks/{self.pb.id}/", {"name": "x"}, format="json").status_code, 404)
        self.assertEqual(c.delete(f"/api/v1/knowledge/benchmarks/{self.bm.id}/").status_code, 404)
        r = c.post("/api/v1/knowledge/analyses/", {"file_id": self.doc.pk, "benchmark_ids": [str(self.bm.id)]}, format="json")
        self.assertEqual(r.status_code, 404)
        # Their own benchmark cannot point at this organisation's document.
        r = c.post("/api/v1/knowledge/benchmarks/", {"name": "Theirs", "file_id": self.policy_doc.pk}, format="json")
        self.assertEqual(r.status_code, 404)

    def test_members_see_and_use_managers_change_guests_neither(self):
        self.assertEqual(self.api(self.member).get("/api/v1/knowledge/playbooks/").status_code, 200)
        self.assertFalse(self.api(self.member).get("/api/v1/knowledge/playbooks/").json()["can_manage"])
        self.assertEqual(self.api(self.member).post("/api/v1/knowledge/playbooks/", {"name": "x"}, format="json").status_code, 403)
        self.assertEqual(self.api(self.member).post(f"/api/v1/knowledge/benchmarks/{self.bm.id}/terms/",
                                                    {"topic": "x", "requirement": "y"}, format="json").status_code, 403)
        self.assertEqual(self.api(self.guest).get("/api/v1/knowledge/playbooks/").status_code, 403)
        self.assertEqual(self.api(self.guest).get("/api/v1/knowledge/benchmarks/").status_code, 403)
        r = self.api(self.manager).post("/api/v1/knowledge/playbooks/", {"name": "NDA", "categoryId": "legal-contracts",
                                                                          "tagIds": ["nda"], "documentType": "Contract"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.json()["category"], r.json()["tags"], r.json()["document_type"]), ("legal-contracts", ["nda"], "Contract"))

    def test_module_can_be_switched_off_for_an_organisation(self):
        # Real bearer tokens: the switch is enforced by middleware, before the view.
        from core.tests.test_security_audit import api, token_for
        from system_settings.models import VerticalSetting
        from system_settings import verticals
        _, mine = token_for(self.manager)
        _, theirs = token_for(self.outsider)
        client = lambda tok: api(tok, HTTP_X_CLIENT_ID=tok.application.client_id)   # noqa: E731
        self.assertEqual(client(mine).get("/api/v1/knowledge/playbooks/").status_code, 200)
        VerticalSetting.objects.create(tenant=self.firm, key="knowledge", enabled=False)
        verticals.invalidate()
        r = client(mine).get("/api/v1/knowledge/playbooks/")
        self.assertEqual((r.status_code, r.json().get("vertical")), (403, "knowledge"))
        self.assertEqual(client(theirs).get("/api/v1/knowledge/playbooks/").status_code, 200)        # others unaffected

    # ── the library (what the Playbooks page needs) ──
    def test_library_filters_versions_and_exports(self):
        c = self.api(self.manager)
        Playbook.objects.create(client=self.firm, name="HR policy pack", category="hr-policies", tags=["hr"])
        self.assertEqual([p["name"] for p in c.get("/api/v1/knowledge/playbooks/?category=legal-contracts").json()["results"]],
                         ["Supplier SLA"])
        self.assertEqual([p["name"] for p in c.get("/api/v1/knowledge/playbooks/?tag=hr").json()["results"]], ["HR policy pack"])
        self.assertEqual(c.get("/api/v1/knowledge/playbooks/?search=SLA").json()["total"], 1)
        r = c.patch(f"/api/v1/knowledge/playbooks/{self.pb.id}/", {"content": "# Guidance"}, format="json")
        self.assertEqual(r.json()["version"], 2)
        md = c.get(f"/api/v1/knowledge/playbooks/{self.pb.id}/export/?as=md")
        self.assertIn(b"## Governing law", md.content)
        self.assertIn("attachment", md["Content-Disposition"])
        self.assertEqual(c.get(f"/api/v1/knowledge/playbooks/{self.pb.id}/export/").json()["positions"][0]["topic"], "Governing law")
        self.assertEqual({t["id"] for t in c.get("/api/v1/knowledge/tags/").json()}, {"sla", "vendors", "hr"})

    def test_upload_goes_through_the_ingestion_pipeline(self):
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=media), \
                mock.patch("core.views.queue_post_upload_processing"):
            f = SimpleUploadedFile("nda-playbook.pdf", b"%PDF-1.4 positions", content_type="application/pdf")
            r = self.api(self.manager).post("/api/v1/knowledge/playbooks/upload/",
                                            {"file": f, "name": "NDA playbook", "categoryId": "legal-contracts"}, format="multipart")
            self.assertEqual(r.status_code, 201, r.content)
            pb = Playbook.objects.get(pk=r.json()["id"])
            self.assertEqual((pb.source_file.project_id, pb.source_file.service_id), ("Knowledge library", "matter"))
            self.assertTrue(Matter.objects.filter(client=self.firm, name="Knowledge library").exists())
            got = self.api(self.member).get(f"/api/v1/knowledge/playbooks/{pb.id}/file/")
            self.assertEqual(got.status_code, 200)
            self.assertEqual(self.api(self.outsider).get(f"/api/v1/knowledge/playbooks/{pb.id}/file/").status_code, 404)

    # ── benchmarks ──
    def test_term_validation(self):
        c = self.api(self.manager)
        url = f"/api/v1/knowledge/benchmarks/{self.bm.id}/terms/"
        bad = [{"topic": "Uptime", "requirement": "x", "comparator": ">="},
               {"topic": "Band", "requirement": "x", "comparator": "between", "target_value": 10, "target_max": 5},
               {"topic": "Neg", "requirement": "x", "comparator": "<=", "target_value": 3, "tolerance": -1},
               {"topic": "Odd", "requirement": "x", "comparator": "about"},
               {"topic": "NaN", "requirement": "x", "comparator": "<=", "target_value": "ten"}]
        for body in bad:
            self.assertEqual(c.post(url, body, format="json").status_code, 400, body)
        r = c.post(url, {"topic": "Credits", "requirement": "Service credits between 5% and 15%", "comparator": "between",
                         "target_value": 5, "target_max": 15, "unit": "%"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)

    @mock.patch(MODEL, return_value={})
    @mock.patch(AGENT)
    def test_suggested_terms_must_quote_the_benchmark(self, agent, _m):
        agent.return_value = {"terms": [
            {"topic": "Availability", "requirement": "At least 99.9%", "comparator": ">=", "target_value": 99.9, "unit": "%",
             "quote": "Availability must be at least 99.9%."},
            {"topic": "Invented", "requirement": "Pay in 10 days", "comparator": "<=", "target_value": 10, "unit": "days",
             "quote": "Invoices are paid within 10 days."}]}
        r = self.api(self.manager).post(f"/api/v1/knowledge/benchmarks/{self.bm.id}/suggest-terms/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([s["topic"] for s in r.json()["suggestions"]], ["Availability"])
        self.assertEqual(agent.call_args.args[0], self.manager)                       # metered for the requester

    # ── links by ID ──
    def test_links_apply_through_matter_and_contract_and_bring_benchmarks(self):
        c = self.api(self.manager)
        self.assertEqual(c.post("/api/v1/knowledge/links/", {"playbook_id": str(self.pb.id), "matter_id": str(self.matter.id)},
                                format="json").status_code, 201)
        self.pb.benchmarks.add(self.bm)                                                # the playbook relies on the policy
        data = self.api(self.member).get(f"/api/v1/knowledge/applicable/?file_id={self.doc.pk}").json()
        self.assertEqual([p["name"] for p in data["linked"]["playbooks"]], ["Supplier SLA"])
        self.assertEqual([b["name"] for b in data["linked"]["benchmarks"]], ["Procurement policy"])
        self.assertEqual(data["matter"]["name"], "Vendor deals")
        # Linking to the contract shows up for the contract and its document.
        other_bm = Benchmark.objects.create(client=self.firm, name="Group SLA standard", kind="sla")
        r = c.post("/api/v1/knowledge/links/", {"benchmark_id": str(other_bm.id), "contract_id": str(self.contract.id)}, format="json")
        self.assertEqual(r.status_code, 201)
        for q in (f"contract_id={self.contract.id}", f"file_id={self.doc.pk}"):
            names = [b["name"] for b in c.get(f"/api/v1/knowledge/applicable/?{q}").json()["linked"]["benchmarks"]]
            self.assertIn("Group SLA standard", names, q)
        # Exactly one source and one target; duplicates are idempotent; members cannot link.
        self.assertEqual(c.post("/api/v1/knowledge/links/", {"benchmark_id": str(other_bm.id), "contract_id": str(self.contract.id)},
                                format="json").status_code, 200)
        self.assertEqual(c.post("/api/v1/knowledge/links/", {"playbook_id": str(self.pb.id)}, format="json").status_code, 400)
        self.assertEqual(self.api(self.member).post("/api/v1/knowledge/links/", {"playbook_id": str(self.pb.id),
                         "file_id": self.doc.pk}, format="json").status_code, 403)
        link_id = r.json()["id"]
        self.assertEqual(self.api(self.outsider).delete(f"/api/v1/knowledge/links/{link_id}/").status_code, 404)
        self.assertEqual(c.delete(f"/api/v1/knowledge/links/{link_id}/").status_code, 204)

    def test_database_enforces_one_source_and_one_target(self):
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            KnowledgeLink.objects.create(client=self.firm, playbook=self.pb, benchmark=self.bm, matter=self.matter)
        with self.assertRaises(IntegrityError), transaction.atomic():
            KnowledgeLink.objects.create(client=self.firm, playbook=self.pb, matter=self.matter, file=self.doc)

    # ── the engine: numbers decided in code ──
    def test_compare_uses_target_and_tolerance(self):
        t = SimpleNamespace(comparator=">=", target_value=Decimal("99.9"), target_max=None, tolerance=Decimal("0.5"))
        self.assertEqual(compare(t, Decimal("99.95"))[0], "compliant")
        self.assertEqual(compare(t, Decimal("99.5")), ("deviation", Decimal("-0.4")))
        self.assertEqual(compare(t, Decimal("99.0"))[0], "red_line")
        band = SimpleNamespace(comparator="between", target_value=Decimal("5"), target_max=Decimal("15"), tolerance=Decimal("1"))
        self.assertEqual(compare(band, Decimal("16"))[0], "deviation")
        self.assertEqual(compare(band, Decimal("2")), ("red_line", Decimal("-3")))

    def test_findings_need_a_verbatim_quote_containing_the_number(self):
        terms = list(BenchmarkTerm.objects.filter(benchmark=self.bm).select_related("benchmark"))
        raw = {"terms": [
            {"id": self.uptime.pk, "found": True, "quote": "available 99.5% of the time in each calendar month", "value": 99.5},
            {"id": self.payment.pk, "found": True, "quote": "pays each invoice within 45 days of receipt", "value": 45},
            {"id": self.response.pk, "found": True, "quote": "Critical incidents are answered within 4 hours", "value": 1},
            {"id": self.law.pk, "found": True, "quote": "governed by the laws of England and Wales", "satisfied": False},
            {"id": self.escrow.pk, "found": True, "quote": "The Supplier deposits source code with an escrow agent"}]}
        by_term = {f["benchmark_term"].pk: f for f in validate(raw, terms, SLA_TEXT)}
        self.assertEqual((by_term[self.uptime.pk]["status"], by_term[self.uptime.pk]["gap"]), ("deviation", Decimal("-0.4")))
        self.assertEqual((by_term[self.payment.pk]["status"], by_term[self.payment.pk]["gap"]), ("red_line", Decimal("15")))
        self.assertEqual(by_term[self.response.pk]["status"], "unverified")           # 1 is not in the quote
        self.assertEqual(by_term[self.law.pk]["status"], "deviation")
        self.assertEqual(by_term[self.escrow.pk]["status"], "unverified")             # quote not in the document
        missing = validate({"terms": [{"id": self.escrow.pk, "found": False}]}, [self.escrow], SLA_TEXT)[0]
        self.assertEqual(missing["status"], "missing")
        absent = BenchmarkTerm(benchmark=self.bm, pk=999, topic="Exclusivity", requirement="No exclusivity", comparator="absent")
        self.assertEqual(validate({"terms": [{"id": 999, "found": False}]}, [absent], SLA_TEXT)[0]["status"], "compliant")

    @mock.patch(MODEL, return_value={"provider": "litellm", "model": "gpt-test"})
    @mock.patch(AGENT)
    def test_analysis_checks_playbook_and_benchmarks_together(self, agent, _m):
        def answer(user, config, messages, task):
            if task == "playbook_review":
                return {"findings": [{"topic": "Governing law", "status": "deviation", "confidence": 0.9,
                                      "quote": "governed by the laws of England and Wales", "explanation": "Not SA law."}]}
            return {"terms": [{"id": self.uptime.pk, "found": True, "value": 99.5,
                               "quote": "available 99.5% of the time in each calendar month"}]}
        agent.side_effect = answer
        colleague = make_user("colleague@kn.test", self.firm, groups=["Client"])   # not on the matter team
        r = self.api(colleague).post("/api/v1/knowledge/analyses/", {"contract_id": str(self.contract.id),
                                     "playbook_id": str(self.pb.id), "benchmark_ids": [str(self.bm.id)]}, format="json")
        self.assertEqual(r.status_code, 404, r.content)          # cannot see this contract
        with mock.patch("contracts.playbook.playbook_review_task.delay") as queue:
            r = self.api(self.manager).post("/api/v1/knowledge/analyses/", {"contract_id": str(self.contract.id),
                                            "playbook_id": str(self.pb.id), "benchmark_ids": [str(self.bm.id)]}, format="json")
        self.assertEqual(r.status_code, 202, r.content)
        queue.assert_called_once()
        self.assertEqual(playbook_review_task.run(r.json()["id"], user_id=self.manager.pk), "done")
        data = self.api(self.manager).get(f"/api/v1/knowledge/analyses/{r.json()['id']}/").json()
        self.assertEqual(data["matter"]["name"], "Vendor deals")
        sources = {f["source"]["type"] for f in data["findings"]}
        self.assertEqual(sources, {"position", "benchmark_term"})
        uptime = next(f for f in data["findings"] if f["source"] == {"type": "benchmark_term", "id": self.uptime.pk})
        self.assertEqual((uptime["status"], uptime["measured_value"], uptime["gap"], uptime["unit"]), ("deviation", 99.5, -0.4, "%"))
        self.assertEqual(data["summary"]["terms"], 5)
        self.assertIn("Supplier SLA, Procurement policy", self.contract.events.last().note)
        # Visible from the document, the contract and the matter; not to another organisation.
        for q in (f"file_id={self.doc.pk}", f"contract_id={self.contract.id}", f"matter_id={self.matter.id}"):
            self.assertEqual(len(self.api(self.manager).get(f"/api/v1/knowledge/analyses/?{q}").json()["results"]), 1, q)
        self.assertEqual(self.api(self.outsider).get(f"/api/v1/knowledge/analyses/{r.json()['id']}/").status_code, 404)

    def test_matter_analysis_queues_each_document_the_person_may_process(self):
        with mock.patch("contracts.playbook.playbook_review_task.delay") as queue:
            r = self.api(self.manager).post("/api/v1/knowledge/analyses/matter/", {"matter_id": str(self.matter.id),
                                            "benchmark_ids": [str(self.bm.id)]}, format="json")
        self.assertEqual(r.status_code, 202, r.content)
        self.assertEqual(r.json()["queued"], 1)
        self.assertEqual(queue.call_count, 1)
        self.assertEqual(PlaybookReview.objects.get().matter, self.matter)
        self.assertEqual(self.api(self.outsider).post("/api/v1/knowledge/analyses/matter/", {"matter_id": str(self.matter.id),
                         "benchmark_ids": [str(self.bm.id)]}, format="json").status_code, 404)

    def test_empty_sources_are_refused(self):
        empty = Playbook.objects.create(client=self.firm, name="Empty")
        c = self.api(self.manager)
        self.assertEqual(c.post("/api/v1/knowledge/analyses/", {"file_id": self.doc.pk}, format="json").status_code, 400)
        self.assertEqual(c.post("/api/v1/knowledge/analyses/", {"file_id": self.doc.pk, "playbook_id": str(empty.id)},
                                format="json").status_code, 400)

    # ── importing the Node library ──
    def test_node_import_is_dry_by_default_maps_organisations_and_is_idempotent(self):
        from django.core.management import call_command
        from oauth2_provider.models import Application
        Application.objects.create(user=self.manager, client_id="cid-manager", client_type="confidential",
                                   authorization_grant_type="password", name="manager app")
        rows = [{"id": "11111111-1111-1111-1111-111111111111", "name": "Firm MSA positions", "description": "d", "content": "c",
                 "category_id": "legal-contracts", "tag_ids": ["t1"], "industry": "Legal", "document_type": "Contract",
                 "use_case": "Deviation Analysis", "created_by": "", "is_public": False, "version": 3, "file_name": None,
                 "file_path": None, "metadata": {}, "client_id": "cid-manager"},
                {"id": "22222222-2222-2222-2222-222222222222", "name": "Orphan", "description": "", "content": "",
                 "category_id": "legal", "tag_ids": [], "created_by": "", "client_id": "", "metadata": {}}]

        class Cursor:
            def __init__(self): self.result = []
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, *a): self.result = rows if "FROM playbooks" in sql else [{"id": "t1", "name": "Vendors"}]
            def fetchall(self): return self.result

        class Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self, **k): return Cursor()
            def rollback(self): pass

        with mock.patch("psycopg2.connect", return_value=Conn()):
            out = io.StringIO()
            call_command("import_node_playbooks", "--node-db", "postgresql://x", stdout=out)
            self.assertFalse(Playbook.objects.filter(legacy_id__startswith="1111").exists())
            self.assertIn("SKIP 'Orphan'", out.getvalue())
            for _ in range(2):
                call_command("import_node_playbooks", "--node-db", "postgresql://x", "--apply", stdout=io.StringIO())
        imported = Playbook.objects.filter(legacy_id="11111111-1111-1111-1111-111111111111")
        self.assertEqual(imported.count(), 1)
        pb = imported.get()
        self.assertEqual((pb.client, pb.name, pb.tags, pb.version, pb.use_case), (self.firm, "Firm MSA positions", ["Vendors"], 3,
                                                                                   "Deviation Analysis"))
        self.assertFalse(Playbook.objects.filter(name="Orphan").exists())
