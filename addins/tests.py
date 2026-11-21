"""Office add-ins: Outlook filing through the shared ingestion pipeline with matter suggestions, Word save as a new
version (legal hold and check-out respected) or a new document, the clause library; one organisation only."""
import os
import tempfile

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import DocumentCheckout, File, LegalHold, Matter, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client
from document_versioning.models import DocumentVersion
from .models import FilingHint

EML = b"From: ann@acme.test\r\nTo: legal@firm.test\r\nSubject: Falcon SPA comments\r\n\r\nPlease see attached.\r\n"


class AddinTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Addin firm")
        cls.other = Client.objects.create(name="Other addin firm")
        cls.lawyer = make_user("lawyer@addin.test", cls.firm, groups=["Client"])
        cls.colleague = make_user("colleague@addin.test", cls.firm, groups=["Client"])
        cls.guest = make_user("guest@addin.test", cls.firm, groups=["Guest"])
        cls.outsider = make_user("outsider@otheraddin.test", cls.other, groups=["Admin"])
        cls.falcon = Matter.objects.create(client=cls.firm, name="Project Falcon", reference="FAL-001", created_by=cls.lawyer)
        cls.falcon.members.add(cls.lawyer)
        cls.atlas = Matter.objects.create(client=cls.firm, name="Atlas", created_by=cls.lawyer)
        cls.atlas.members.add(cls.lawyer)
        cls.theirs = Matter.objects.create(client=cls.other, name="Project Falcon", created_by=cls.outsider)

    def setUp(self):
        cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        media = override_settings(MEDIA_ROOT=self.tmp.name)
        media.enable()
        self.addCleanup(media.disable)

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def file_email(self, user=None, matter=None, **extra):
        body = {"matter_id": str((matter or self.falcon).id), "subject": "Falcon SPA comments", "sender": "Ann <ann@acme.test>",
                "sent_at": "2026-11-20T09:30:00Z", "email": SimpleUploadedFile("message.eml", EML, content_type="message/rfc822"),
                "attachments": [SimpleUploadedFile("SPA markup.docx", b"PK\x03\x04 markup", content_type="application/octet-stream")],
                **extra}
        return self.api(user or self.lawyer).post("/api/v1/addins/outlook/file/", body, format="multipart")

    def test_filing_uses_the_matter_pipeline_and_learns_the_sender(self):
        r = self.file_email()
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["filed"], 2)
        names = sorted(File.objects.filter(project_id="Project Falcon", user=self.lawyer).values_list("filename", flat=True))
        self.assertEqual(names, ["2026-11-20 0930 Falcon SPA comments.eml", "SPA markup.docx"])
        stored = File.objects.get(filename__endswith=".eml")
        self.assertEqual(stored.service_id, "matter")
        self.assertTrue(stored.filepath.startswith(self.tmp.name))
        self.assertEqual(FilingHint.objects.get(sender_domain="acme.test").matter, self.falcon)
        # Filing the same email again does not duplicate it.
        self.file_email()
        self.assertEqual(File.objects.filter(filename__endswith=".eml").count(), 1)

    def test_suggestions_rank_by_reference_name_and_history(self):
        self.file_email(matter=self.atlas)
        r = self.api(self.lawyer).get("/api/v1/addins/outlook/suggest/", {"subject": "RE: FAL-001 completion", "sender": "x@acme.test"})
        results = r.json()["results"]
        self.assertEqual(results[0]["name"], "Project Falcon")
        self.assertIn("matter reference in the subject", results[0]["reasons"])
        r = self.api(self.lawyer).get("/api/v1/addins/outlook/suggest/", {"subject": "Invoice", "sender": "bob@acme.test"})
        self.assertEqual(r.json()["results"][0]["name"], "Atlas")       # where acme.test email was filed before
        # Never another organisation's matters, nor matters the person cannot see.
        self.assertEqual([m["name"] for m in self.api(self.colleague).get("/api/v1/addins/outlook/suggest/",
                                                                           {"subject": "Project Falcon"}).json()["results"]], [])

    def test_filing_is_scoped_and_permissioned(self):
        self.assertEqual(self.file_email(user=self.outsider).status_code, 404)       # another organisation's matter
        self.assertEqual(self.file_email(user=self.colleague).status_code, 404)      # not on the matter
        self.assertEqual(self.file_email(user=self.guest).status_code, 403)          # read-only role
        self.assertEqual(self.file_email(matter=self.theirs).status_code, 404)
        big = SimpleUploadedFile("huge.eml", b"x" * (51 * 1024 * 1024))
        self.assertEqual(self.file_email(email=big).status_code, 400)

    def stored_docx(self):
        path = os.path.join(self.tmp.name, "uploads", "draft.docx")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"PK\x03\x04 version one")
        return File.objects.create(user=self.lawyer, run=Run.objects.create(user=self.lawyer), filename="draft.docx",
                                   filepath=path, project_id="Project Falcon", service_id="matter", md5_hash="v1", content="old text")

    def save(self, user, **body):
        body.setdefault("document", SimpleUploadedFile("draft.docx", b"PK\x03\x04 version two"))
        return self.api(user).post("/api/v1/addins/word/save/", body, format="multipart")

    def test_word_save_keeps_the_previous_version(self):
        doc = self.stored_docx()
        r = self.save(self.lawyer, file_id=doc.pk)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["version"], 1)
        with open(doc.filepath, "rb") as fh:
            self.assertEqual(fh.read(), b"PK\x03\x04 version two")
        version = DocumentVersion.objects.get(file=doc)
        with open(version.filepath, "rb") as fh:
            self.assertEqual(fh.read(), b"PK\x03\x04 version one")
        doc.refresh_from_db()
        self.assertEqual(doc.content, "")                                   # stale text cleared

    def test_word_save_respects_access_holds_and_check_out(self):
        doc = self.stored_docx()
        self.assertEqual(self.save(self.colleague, file_id=doc.pk).status_code, 404)
        self.assertEqual(self.save(self.outsider, file_id=doc.pk).status_code, 404)
        DocumentCheckout.objects.create(file=doc, user=self.colleague)
        self.assertEqual(self.save(self.lawyer, file_id=doc.pk).status_code, 409)
        DocumentCheckout.objects.all().delete()
        hold = LegalHold.objects.create(client=self.firm, name="Hold", created_by=self.lawyer)
        hold.files.add(doc)
        self.assertEqual(self.save(self.lawyer, file_id=doc.pk).status_code, 403)
        with open(doc.filepath, "rb") as fh:
            self.assertEqual(fh.read(), b"PK\x03\x04 version one")       # untouched

    def test_word_save_as_new_document_in_a_matter(self):
        r = self.save(self.lawyer, matter_id=str(self.falcon.id), filename="Heads of terms")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertTrue(File.objects.filter(filename="Heads of terms.docx", project_id="Project Falcon").exists())
        self.assertEqual(self.save(self.lawyer, matter_id=str(self.theirs.id)).status_code, 404)

    def test_clause_library_is_the_organisations_own(self):
        from document_automation.models import Clause
        Clause.objects.create(title="Limitation of liability", content="Liability is capped...", project_id="p", service_id="s",
                              client_name=self.colleague.username)
        Clause.objects.create(title="Their clause", content="Secret", project_id="p", service_id="s", client_name=self.outsider.username)
        titles = [c["title"] for c in self.api(self.lawyer).get("/api/v1/addins/word/clauses/").json()["results"]]
        self.assertEqual(titles, ["Limitation of liability"])
