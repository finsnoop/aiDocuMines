"""explain_document_access: says which rule shows each document, and flags unlinked (legacy) documents."""
import io

from django.core.management import call_command
from django.test import TestCase

from core.models import File, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client


class ExplainDocumentAccessTests(TestCase):
    def test_reports_rule_and_unlinked_state(self):
        firm = Client.objects.create(name="Explain firm")
        other = Client.objects.create(name="Explain other")
        admin = make_user("admin@explain.test", firm, groups=["Admin"])
        member = make_user("member@explain.test", firm, groups=["Client"])
        outsider = make_user("someone@explainother.test", other, groups=["Client"])
        root = make_user("root@explain.test", firm, superuser=True)
        for owner, name in ((member, "member-note.pdf"), (outsider, "other-org.pdf")):
            File.objects.create(user=owner, run=Run.objects.create(user=owner), filename=name, filepath="/x/" + name,
                                project_id="P", service_id="matter", md5_hash=name)
        out = io.StringIO()
        call_command("explain_document_access", "--viewer", admin.email, stdout=out)
        text = out.getvalue()
        self.assertIn("organisation admin=True", text)
        self.assertIn("member-note.pdf", text)
        self.assertIn("organisation admin (sees every document", text)
        self.assertNotIn("other-org.pdf", text)                       # never another organisation
        self.assertIn("legacy, unlinked", text)
        out = io.StringIO()
        call_command("explain_document_access", "--viewer", root.email, "--filename", "other-org", stdout=out)
        self.assertIn("PLATFORM SUPERUSER", out.getvalue())
        out = io.StringIO()
        call_command("explain_document_access", "--viewer", member.email, stdout=out)
        self.assertIn("own document", out.getvalue())
        self.assertNotIn("UNEXPLAINED", out.getvalue())
