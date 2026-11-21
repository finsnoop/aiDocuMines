"""load_demo_corpus on a small synthetic corpus: checks checksums first, then creates the organisation, people (as
Users & access does), matters with teams, documents through the ingestion pipeline, contracts, the playbook,
frozen benchmarks, a legal hold, an ethical wall and a second organisation for isolation tests."""
import hashlib
import io
import json
import os
import tempfile
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from core.tests.test_tenancy import make_user
from custom_authentication.models import Client


def write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return hashlib.sha256(data).hexdigest()


class LoadDemoCorpusTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        o = override_settings(MEDIA_ROOT=self.media.name)
        o.enable()
        self.addCleanup(o.disable)
        for target in ("custom_authentication.views_admin._register_sign_in", "core.views.queue_post_upload_processing"):
            p = mock.patch(target, return_value=True)
            p.start()
            self.addCleanup(p.stop)
        self.root = self.tmp.name
        home = Client.objects.create(name="AI DocuMines")
        self.boss = make_user("boss@datasqan.test", home, superuser=True)
        docs = [("TEC-001-007", "TEC-001", "Service Level Agreement and Service Credits", "contract", "Executed",
                 "ZAR 18,600,000 over 36 months", "01_Commercial/TEC-001-007__SLA.pdf"),
                ("TEC-001-008", "TEC-001", "Data Processing Agreement - POPIA GDPR", "contract", "Draft", "",
                 "01_Commercial/TEC-001-008__DPA.docx"),
                ("TEC-001-014", "TEC-001", "Legal Negotiation Playbook", "policy", "Final", "",
                 "23_Fixtures/TEC-001-014__Playbook.docx"),
                ("IP-003-001", "IP-003", "Patent Claim Chart", "schedule", "Draft", "", "03_IP/IP-003-001__Chart.docx"),
                ("MA-002-001", "MA-002", "Share Purchase Agreement - Draft v1", "contract", "Draft", "USD 42 million",
                 "02_MA/MA-002-001__SPA.docx")]
        self.rows = []
        for doc_id, matter_id, title, kind, status, value, rel in docs:
            digest = write(self.root, rel, f"{title} body text for {doc_id}".encode())
            self.rows.append({"doc_id": doc_id, "matter_id": matter_id, "title": title, "kind": kind, "status": status,
                              "value": value, "filename": rel, "party_2": "Copperline Cloud Services (Pty) Ltd",
                              "record_date": "2026-04-15", "focus": "test", "sha256": digest})
        graph = [{"matter_id": "TEC-001", "matter": "Nimbus Platform Procurement", "vertical": "Commercial / SaaS",
                  "parties": ["Kestrel", "Copperline"], "facts": ["Fact one"], "known_test_discrepancies": ["Issue one"]},
                 {"matter_id": "IP-003", "matter": "Aurora v Bluewave Lens-Array Dispute", "vertical": "IP Litigation"},
                 {"matter_id": "MA-002", "matter": "Arden Diagnostics Acquisition", "vertical": "Private Equity / M&A"}]
        self.save(graph)

    def save(self, graph=None):
        json.dump(self.rows, open(os.path.join(self.root, "index.json"), "w", encoding="utf-8"))
        if graph is not None:
            json.dump(graph, open(os.path.join(self.root, "matter_graph.json"), "w", encoding="utf-8"))

    def run_cmd(self, *extra):
        out = io.StringIO()
        call_command("load_demo_corpus", "--corpus", self.root, "--as", self.boss.email, "--password", "Demo-Pass-2026!",
                     *extra, stdout=out)
        return out.getvalue()

    def test_check_only_creates_nothing_and_catches_tampered_files(self):
        self.assertIn("Check passed", self.run_cmd())
        self.assertFalse(Client.objects.filter(name="Datasqan Demo Law Firm").exists())
        self.rows[0]["sha256"] = "0" * 64
        self.save()
        with self.assertRaises(CommandError):
            self.run_cmd()

    def test_execute_builds_the_whole_demo(self):
        from contracts.models import Benchmark, Contract, KnowledgeLink, Playbook
        from core.models import EthicalWall, File, LegalHold, Matter
        from custom_authentication.models import CustomUser
        text = self.run_cmd("--execute")
        firm = Client.objects.get(name="Datasqan Demo Law Firm")
        self.assertTrue(Client.objects.filter(name="Isolation Test Firm").exists())
        people = CustomUser.objects.filter(client=firm)
        self.assertEqual(people.count(), 7)
        partner = people.get(email="james.partner@demo.datasqan.com")
        self.assertTrue(partner.check_password("Demo-Pass-2026!"))
        self.assertEqual(list(partner.groups.values_list("name", flat=True)), ["Manager"])
        self.assertIn("Demo-Pass-2026!", text)
        tec = Matter.objects.get(client=firm, reference="TEC-001")
        self.assertEqual(tec.name, "Nimbus Platform Procurement")
        self.assertIn("Fact one", tec.description)
        self.assertEqual(File.objects.filter(user=partner, project_id=tec.name, service_id="matter").count(), 3)
        self.assertEqual(File.objects.filter(user=partner, service_id="matter").count(), 5)
        spa = Contract.objects.get(client=firm, title__startswith="Share Purchase")
        self.assertEqual((spa.contract_type, spa.value, spa.currency, spa.stage), ("SPA", 42_000_000, "USD", "draft"))
        sla = Contract.objects.get(client=firm, title__startswith="Service Level")
        self.assertEqual((sla.stage, sla.currency), ("active", "ZAR"))
        pb = Playbook.objects.get(client=firm)
        self.assertEqual(pb.positions.count(), 6)
        self.assertEqual(pb.source_file.filename, "TEC-001-014__Playbook.docx")
        self.assertEqual(Benchmark.objects.filter(client=firm).count(), 2)
        b = Benchmark.objects.get(client=firm, kind="sla")
        self.assertEqual((b.bucket.name, b.file.service_id, b.terms.count()), ("Service levels", "benchmark", 2))
        self.assertEqual(KnowledgeLink.objects.filter(client=firm, matter=tec).count(), 3)
        self.assertTrue(LegalHold.objects.filter(client=firm, matter__reference="IP-003").exists())
        wall = EthicalWall.objects.get(client=firm, matter__reference="MA-002")
        self.assertEqual(list(wall.excluded.values_list("email", flat=True)), ["sipho.paralegal@demo.datasqan.com"])
        paralegal = people.get(email="sipho.paralegal@demo.datasqan.com")
        self.assertTrue(Matter.objects.filter(reference="IP-003", members=paralegal).exists())   # first half of matters
        # Running again is refused (the organisation exists).
        with self.assertRaises(CommandError):
            self.run_cmd("--execute")
