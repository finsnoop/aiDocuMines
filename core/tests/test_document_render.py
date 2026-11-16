"""Every stored document must open in the viewer: as itself, a converted rendition, or a structured view."""
import io
import os
import shutil
import tempfile
import zipfile

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.document_render import render_content, render_manifest
from core.models import File, Run
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client

TMP_MEDIA = tempfile.mkdtemp(prefix="render-media-")
NO_ONLYOFFICE = {"DOC_SERVER_URL": "", "JWT_SECRET": "", "DOWNLOAD_TTL": 300, "HTTP_CONNECT_TIMEOUT": 1}


@override_settings(MEDIA_ROOT=TMP_MEDIA, ONLYOFFICE=NO_ONLYOFFICE)
class RenderManifestTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Firm A")
        cls.user = make_user("r1@firm-a.test", cls.firm, groups=["Client"])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="render-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _file(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        run = Run.objects.create(user=self.user, status="Completed")
        return File.objects.create(filename=name, filepath=path, user=self.user, run=run,
                                   project_id="P", service_id="s", md5_hash=f"{name}-{os.urandom(4).hex()}"), path

    def _manifest(self, name, data):
        f, path = self._file(name, data)
        return render_manifest(f, path)

    def test_native_types_are_served_as_they_are(self):
        m = self._manifest("scan.png", b"\x89PNG\r\n\x1a\n" + b"0" * 20)
        self.assertEqual((m["kind"], m["content_type"]), ("image", "image/png"))
        self.assertEqual(self._manifest("call.mp3", b"ID3....")["kind"], "media")

    def test_csv_becomes_a_table_with_header(self):
        m = self._manifest("people.csv", "name,city\nAda,London\nGrace,Arlington\n")
        sheet = m["sheets"][0]
        self.assertEqual(m["kind"], "table")
        self.assertEqual(sheet["columns"], ["name", "city"])
        self.assertEqual(sheet["rows"], [["Ada", "London"], ["Grace", "Arlington"]])

    def test_excel_becomes_a_table_when_it_cannot_be_converted(self):
        import openpyxl
        wb = openpyxl.Workbook()
        wb.active.title = "Fees"
        wb.active.append(["Matter", "Amount"])
        wb.active.append(["Atlas", 1200])
        buf = io.BytesIO()
        wb.save(buf)
        m = self._manifest("fees.xlsx", buf.getvalue())
        self.assertIn(m["kind"], ("pdf", "table"))
        if m["kind"] == "table":
            self.assertEqual(m["sheets"][0]["name"], "Fees")
            self.assertEqual(m["sheets"][0]["rows"], [["Atlas", "1200"]])

    def test_powerpoint_shows_slide_text_when_it_cannot_be_converted(self):
        from pptx import Presentation
        prs = Presentation()
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = "Quarterly review"
        s.placeholders[1].text = "Revenue up"
        buf = io.BytesIO()
        prs.save(buf)
        m = self._manifest("deck.pptx", buf.getvalue())
        self.assertIn(m["kind"], ("pdf", "slides"))
        if m["kind"] == "slides":
            self.assertEqual(m["slides"][0]["title"], "Quarterly review")
            self.assertIn("Revenue up", m["slides"][0]["text"])

    def test_html_is_defanged(self):
        m = self._manifest("page.html", '<h1 onclick="steal()">Hi</h1><script>alert(1)</script>'
                                        '<a href="javascript:alert(2)">x</a><iframe src="https://e.test"></iframe>')
        self.assertEqual(m["kind"], "html")
        html = m["html"].lower()
        for bad in ("<script", "onclick", "javascript:", "<iframe"):
            self.assertNotIn(bad, html)
        self.assertIn("<h1>hi</h1>", html)

    def test_markdown_is_rendered(self):
        m = self._manifest("notes.md", "# Title\n\nSome *text*")
        self.assertEqual(m["kind"], "html")
        self.assertIn("<h1>Title</h1>", m["html"])

    def test_code_and_data_files_are_text(self):
        m = self._manifest("config.json", '{"a": 1}')
        self.assertEqual((m["kind"], m["language"]), ("text", "json"))
        self.assertEqual(self._manifest("README", "plain words")["kind"], "text")

    def test_email_shows_headers_body_and_attachments(self):
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = "a@x.test", "b@x.test", "Contract"
        msg.set_content("Please review.")
        msg.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="draft.pdf")
        m = self._manifest("mail.eml", msg.as_bytes())
        self.assertEqual(m["kind"], "email")
        self.assertEqual(m["headers"]["Subject"], "Contract")
        self.assertIn("Please review.", m["text"])
        self.assertEqual(m["attachments"][0]["name"], "draft.pdf")

    def test_zip_lists_entries(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("a/one.txt", "1")
            z.writestr("two.txt", "22")
        m = self._manifest("bundle.zip", buf.getvalue())
        self.assertEqual(m["kind"], "archive")
        self.assertEqual([e["name"] for e in m["entries"]], ["a/one.txt", "two.txt"])

    def test_svg_and_tiff_become_pdf(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>'
        f, path = self._file("logo.svg", svg)
        m = render_manifest(f, path)
        self.assertEqual(m["kind"], "pdf")
        out, ctype = render_content(f, path)
        self.assertEqual(ctype, "application/pdf")
        with open(out, "rb") as fh:
            self.assertTrue(fh.read(5).startswith(b"%PDF"))
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (4, 4), "red").save(buf, "TIFF")
        self.assertEqual(self._manifest("scan.tiff", buf.getvalue())["kind"], "pdf")

    def test_unknown_binary_shows_details(self):
        m = self._manifest("model.bin", b"\x00\x01\x02" * 100 + b"Readable label inside the file" + b"\x00" * 50)
        self.assertEqual(m["kind"], "details")
        labels = [f["label"] for f in m["facts"]]
        self.assertIn("Size", labels)


@override_settings(MEDIA_ROOT=TMP_MEDIA, ONLYOFFICE=NO_ONLYOFFICE)
class RenderEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm, cls.other = Client.objects.create(name="Firm A"), Client.objects.create(name="Firm B")
        cls.user = make_user("e1@firm-a.test", cls.firm, groups=["Client"])
        cls.stranger = make_user("e2@firm-b.test", cls.other, groups=["Client"])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="render-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        path = os.path.join(self.dir, "data.csv")
        with open(path, "w") as fh:
            fh.write("a,b\n1,2\n")
        run = Run.objects.create(user=self.user, status="Completed")
        self.file = File.objects.create(filename="data.csv", filepath=path, user=self.user, run=run,
                                        project_id="P", service_id="s", md5_hash=os.urandom(6).hex())

    def _get(self, user, suffix=""):
        c = APIClient()
        c.force_authenticate(user=user)
        return c.get(f"/api/v1/core/files/{self.file.id}/render/{suffix}")

    def test_owner_gets_the_manifest(self):
        r = self._get(self.user)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["kind"], "table")

    def test_other_tenant_cannot_see_it(self):
        self.assertEqual(self._get(self.stranger).status_code, 404)
        self.assertEqual(self._get(self.stranger, "content/").status_code, 404)

    def test_content_is_refused_for_structured_kinds(self):
        self.assertEqual(self._get(self.user, "content/").status_code, 415)

    def test_missing_storage_is_explained(self):
        os.remove(self.file.filepath)
        r = self._get(self.user)
        self.assertEqual(r.status_code, 404)
        self.assertIn("missing from storage", r.json()["error"])
