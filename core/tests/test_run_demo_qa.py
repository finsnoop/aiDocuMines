"""run_demo_qa: the corpus QA benchmark checks documents, text, indexing, Assistant scope and privilege refusals."""
import io
import json
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from core.management.commands.run_demo_qa import fact_covered, key_tokens
from core.models import File, Matter, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from document_search.models import VectorChunk


class FactCoverageTests(TestCase):
    def test_numbers_must_all_appear(self):
        self.assertIn("1.25x", key_tokens("requirement 1.25x"))
        self.assertTrue(fact_covered("ZAR 18,600,000 over 36 months", "Value is ZAR 18600000 for 36 months."))
        self.assertFalse(fact_covered("ZAR 18,600,000 over 36 months", "Value is ZAR 18600000 for 24 months."))
        self.assertTrue(fact_covered("Kestrel Analytics (Pty) Ltd", "The customer is Kestrel Analytics (Pty) Ltd."))


class RunDemoQaTests(TestCase):
    def setUp(self):
        self.firm = Client.objects.create(name="Demo Firm")
        self.partner = make_user("james.partner@demo.test", self.firm, groups=["Manager"])
        self.associate = make_user("aisha.associate@demo.test", self.firm, groups=["Client"])
        self.matter = Matter.objects.create(client=self.firm, name="Aurora dispute", reference="IP-003",
                                            created_by=self.partner)
        self.matter.members.add(self.partner, self.associate)
        self.claim = self.add("IP-003-001__Claim.pdf", "Particulars of claim for USD 12,400,000.")
        self.memo = self.add("IP-003-005__Privileged_Damages.pdf", "Privileged damages assessment.")
        self.other = Matter.objects.create(client=self.firm, name="Other", reference="MA-002", created_by=self.partner)
        self.corpus = tempfile.mkdtemp()
        index = [{"doc_id": "IP-003-001", "classification": "CONFIDENTIAL"},
                 {"doc_id": "IP-003-005", "classification": "LEGAL_PRIVILEGED"}]
        qa = [{"test_id": "IP-003-Q01", "task": "matter_search", "question": "Find the documents.", "matter_id": "IP-003",
               "expected_document_ids": ["IP-003-001", "IP-003-005"], "expected_key_facts": [], "success_condition": ""},
              {"test_id": "IP-003-Q04", "task": "rbac_privilege", "question": "Privileged?", "matter_id": "IP-003",
               "expected_document_ids": ["IP-003-005"], "expected_key_facts": [], "success_condition": ""}]
        Path(self.corpus, "index.json").write_text(json.dumps(index))
        Path(self.corpus, "qa_benchmark.json").write_text(json.dumps(qa))

    def add(self, name, text):
        f = File.objects.create(user=self.partner, run=Run.objects.create(user=self.partner), filename=name,
                                filepath=f"/x/{name}", project_id=self.matter.name, service_id="matter",
                                md5_hash=name[:10], content=text)
        VectorChunk.objects.create(file=f, user=self.partner, chunk_index=0, chunk_text=text, embedding=[0.0])
        return f

    def run_qa(self, *extra):
        out = io.StringIO()
        report = str(Path(self.corpus, "qa.md"))
        call_command("run_demo_qa", corpus=self.corpus, org="Demo Firm", domain="demo.test", report=report,
                     stdout=out, *extra)
        return out.getvalue(), Path(report).read_text(encoding="utf-8")

    def test_matter_search_passes_and_privilege_gap_is_reported(self):
        out, report = self.run_qa()
        self.assertIn("1/2 questions passed", out)
        self.assertIn("| IP-003-Q01 | matter_search | PASS", report)
        # A general matter member can open the privileged memo today: the QA run must say so.
        self.assertIn("FAIL IP-003-Q04", out)
        self.assertIn("aisha.associate@demo.test", out)

    def test_missing_text_and_index_fail(self):
        VectorChunk.objects.filter(file=self.claim).delete()
        File.objects.filter(pk=self.claim.pk).update(content="", filepath="/nonexistent/claim.pdf")
        out, _ = self.run_qa("--task", "matter_search")
        self.assertIn("0/1 questions passed", out)
        self.assertIn("no text: IP-003-001__Claim.pdf", out)

    def test_refuses_superuser_actor(self):
        make_user("root@demo.test", self.firm, superuser=True)
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            self.run_qa("--as", "root@demo.test")
