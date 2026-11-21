"""Watermarks on every format that can carry one (Word, Excel, PowerPoint, images, PDF) and on every route a copy
leaves by: download, the in-app viewer and read-only OnlyOffice. Stamped files must still open in the usual
libraries, keep their content, and the stored document must never change. Formats that cannot carry a mark follow
the organisation's choice (allow and log, or block)."""
import io
import tempfile
import zipfile
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from core import watermark
from core.models import File, Run
from core.tests.test_checkout_watermark import SCOPE, make_pdf
from core.tests.test_tenancy import make_user
from custom_authentication.models import Client, OrganisationPolicy
from document_operations.models import EffectiveAccess

MARK = "Confidential · viewer@wm.test · 2026-11-21 10:00"
ONLYOFFICE = {"DOC_SERVER_URL": "https://ds.test", "DOC_SERVER_PUBLIC_URL": "https://ds.test",
              "JWT_SECRET": "test-secret-for-editor-config-1234567890", "DOWNLOAD_TTL": 300,
              "HTTP_CONNECT_TIMEOUT": 5, "HTTP_READ_TIMEOUT": 5}


def make_docx(path, *, header=None, sections=1):
    import docx
    from docx.enum.section import WD_SECTION
    d = docx.Document()
    d.add_paragraph("Body of the agreement")
    if header:
        d.sections[0].header.paragraphs[0].text = header
    for i in range(1, sections):
        d.add_section(WD_SECTION.NEW_PAGE)
        d.add_paragraph(f"Schedule {i}")
    d.save(path)


def make_xlsx(path):
    import openpyxl
    from openpyxl.worksheet.table import Table
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Fees"
    ws.append(["Item", "Amount"])
    ws.append(["Retainer", 1000])
    ws.oddHeader.left.text = "Kestrel & Co"
    ws.add_table(Table(displayName="FeeTable", ref="A1:B2"))         # tableParts must stay after headerFooter
    wb.create_sheet("Notes").append(["Plain sheet"])
    wb.save(path)


def make_pptx(path):
    import pptx
    p = pptx.Presentation()
    for title in ("Deal overview", "Valuation"):
        s = p.slides.add_slide(p.slide_layouts[1])
        s.shapes.title.text = title
    p.save(path)


def make_image(path, fmt):
    from PIL import Image
    Image.new("RGB", (600, 400), (255, 255, 255)).save(path, format=fmt)


class FormatTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_word_every_header_marked_and_document_still_opens(self):
        import docx
        path = self.dir / "a.docx"
        make_docx(path, header="Kestrel letterhead", sections=3)
        original = path.read_bytes()
        out = self.dir / "out.docx"
        out.write_bytes(watermark.stamp(path, MARK))
        d = docx.Document(out)
        self.assertEqual(d.paragraphs[0].text, "Body of the agreement")
        first = d.sections[0]
        self.assertIn("Kestrel letterhead", first.header.paragraphs[0].text)          # kept
        self.assertIn(MARK, "\n".join(p.text for p in first.header.paragraphs))
        with zipfile.ZipFile(out) as z:
            headers = [n for n in z.namelist() if n.startswith("word/header")]
            xml = "".join(z.read(n).decode() for n in headers)
        self.assertIn("AiDocuMinesWatermark", xml)                                     # the diagonal Word watermark
        self.assertIn('rotation:315', xml)
        self.assertEqual(path.read_bytes(), original)                                  # source untouched

    def test_word_without_headers_gets_one_with_content_type_and_relationship(self):
        import docx
        path = self.dir / "b.docx"
        make_docx(path)
        with zipfile.ZipFile(path) as z:
            had_header = any(n.startswith("word/header") for n in z.namelist())
        data = watermark.stamp(path, MARK)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            types = z.read("[Content_Types].xml").decode()
            rels = z.read("word/_rels/document.xml.rels").decode()
        if not had_header:
            self.assertIn("header_aidoc1.xml", types)
            self.assertIn("rIdAidocWm1", rels)
        d = docx.Document(io.BytesIO(data))
        self.assertIn(MARK, "\n".join(p.text for p in d.sections[0].header.paragraphs))

    def test_excel_header_and_footer_on_every_sheet_keeping_left_section_and_tables(self):
        import openpyxl
        path = self.dir / "c.xlsx"
        make_xlsx(path)
        out = self.dir / "out.xlsx"
        out.write_bytes(watermark.stamp(path, MARK))
        wb = openpyxl.load_workbook(out)
        for ws in wb.worksheets:
            self.assertIn(MARK, ws.oddHeader.center.text, ws.title)
            self.assertIn(MARK, ws.oddFooter.center.text, ws.title)
        self.assertEqual(wb["Fees"].oddHeader.left.text, "Kestrel & Co")
        self.assertEqual(wb["Fees"]["B2"].value, 1000)
        self.assertIn("FeeTable", wb["Fees"].tables)
        with zipfile.ZipFile(out) as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode()
        self.assertLess(sheet.index("<headerFooter"), sheet.index("<tableParts"))     # schema order kept

    def test_excel_escapes_ampersands_in_the_mark(self):
        import openpyxl
        path = self.dir / "d.xlsx"
        make_xlsx(path)
        out = self.dir / "out.xlsx"
        out.write_bytes(watermark.stamp(path, "Smith & Jones · a@b.test"))
        # Excel header codes write a literal & as && (displayed as "Smith & Jones"); a lone & would start a code.
        self.assertIn("Smith && Jones", openpyxl.load_workbook(out).active.oddHeader.center.text)

    def test_powerpoint_every_slide_marked_with_unique_shape_ids(self):
        import pptx
        path = self.dir / "e.pptx"
        make_pptx(path)
        out = self.dir / "out.pptx"
        out.write_bytes(watermark.stamp(path, MARK))
        for slide in pptx.Presentation(out).slides:
            names = [s.name for s in slide.shapes]
            self.assertIn("Watermark", names)
            marks = [s for s in slide.shapes if s.name == "Watermark"]
            self.assertEqual(marks[0].text_frame.text, MARK)
            ids = [s.shape_id for s in slide.shapes]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertTrue(slide.shapes.title.text)                                  # content kept

    def test_images_are_stamped_in_their_own_format(self):
        from PIL import Image
        for ext, fmt in ((".png", "PNG"), (".jpg", "JPEG"), (".webp", "WEBP"), (".gif", "GIF")):
            with self.subTest(fmt=fmt):
                path = self.dir / f"f{ext}"
                make_image(path, fmt)
                data = watermark.stamp(path, MARK)
                with Image.open(io.BytesIO(data)) as img:
                    self.assertEqual((img.format, img.size), (fmt, (600, 400)))
                    colours = img.convert("RGB").getcolors(maxcolors=1_000_000)
                self.assertGreater(len(colours), 1)                                   # something was drawn

    def test_zip_bombs_and_unsupported_formats_are_not_stamped(self):
        path = self.dir / "bomb.docx"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", b"0" * (watermark.MAX_OOXML_BYTES + 1))
        with self.assertRaises(ValueError):
            watermark.stamp(path, MARK)
        with self.assertRaises(ValueError):
            watermark.stamp(self.dir / "notes.csv", MARK)
        self.assertFalse(watermark.can_stamp("old.doc"))
        self.assertTrue(watermark.can_stamp("deck.PPTX"))


class RouteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.firm = Client.objects.create(name="Watermark firm")
        cls.owner = make_user("owner@wm.test", cls.firm, groups=["Client"])
        cls.viewer = make_user("viewer@wm.test", cls.firm, groups=["Client"])

    def setUp(self):
        cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        OrganisationPolicy.objects.update_or_create(client=self.firm, defaults={"watermark_downloads": True})

    def document(self, name, maker):
        path = Path(self.tmp.name) / name
        maker(path)
        f = File.objects.create(user=self.owner, run=Run.objects.create(user=self.owner), filename=name,
                                filepath=str(path), project_id="P", service_id="matter", md5_hash=name)
        EffectiveAccess.objects.create(user=self.viewer, file=f, can_download=True)
        return f, path

    def get(self, user, url):
        c = APIClient()
        c.force_authenticate(user)
        with mock.patch(SCOPE, return_value=True):
            r = c.get(url)
        return r, (b"".join(r.streaming_content) if getattr(r, "streaming", False) else r.content)

    def test_word_download_is_stamped_for_the_viewer_and_storage_is_untouched(self):
        f, path = self.document("terms.docx", make_docx)
        before = path.read_bytes()
        r, data = self.get(self.viewer, f"/api/v1/core/download/?file_id={f.pk}")
        self.assertEqual(r.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            headers = "".join(z.read(n).decode() for n in z.namelist() if n.startswith("word/header"))
        self.assertIn("viewer@wm.test", headers)
        self.assertEqual(path.read_bytes(), before)
        self.assertIn("attachment", r["Content-Disposition"])
        self.assertEqual(r["Cache-Control"], "private, no-store")

    def test_formats_without_a_mark_follow_the_organisation_choice(self):
        f, _ = self.document("ledger.csv", lambda p: p.write_text("a,b\n1,2\n"))
        r, data = self.get(self.viewer, f"/api/v1/core/download/?file_id={f.pk}")
        self.assertEqual((r.status_code, data), (200, b"a,b\n1,2\n"))                  # allow (default): unmarked
        OrganisationPolicy.objects.filter(client=self.firm).update(watermark_unsupported="block")
        r, _ = self.get(self.viewer, f"/api/v1/core/download/?file_id={f.pk}")
        self.assertEqual(r.status_code, 403)
        self.assertIn("cannot carry one", r.json()["error"])
        # Nothing changes when watermarks are off.
        OrganisationPolicy.objects.filter(client=self.firm).update(watermark_downloads=False)
        self.assertEqual(self.get(self.viewer, f"/api/v1/core/download/?file_id={f.pk}")[0].status_code, 200)

    def test_in_app_viewer_copy_is_stamped(self):
        f, _ = self.document("deal.pdf", make_pdf)
        r, data = self.get(self.viewer, f"/api/v1/core/files/{f.pk}/render/content/")
        self.assertEqual(r.status_code, 200)
        import fitz
        with fitz.open(stream=data, filetype="pdf") as doc:
            self.assertIn("viewer@wm.test", doc[0].get_text())
        self.assertEqual(r["Cache-Control"], "private, no-store")

    @override_settings(ONLYOFFICE=ONLYOFFICE)
    def test_read_only_onlyoffice_gets_a_stamped_copy_and_editors_the_original(self):
        f, path = self.document("memo.docx", make_docx)
        r, _ = self.get(self.viewer, f"/api/v1/core/onlyoffice/editor-config/?file_id={f.pk}")
        cfg = r.json()["config"]
        self.assertEqual(cfg["editorConfig"]["mode"], "view")
        url = urlparse(cfg["document"]["url"])
        r = APIClient().get(f"{url.path}?{url.query}")
        self.assertEqual(r.status_code, 200, r.content[:200])
        data = b"".join(r.streaming_content) if getattr(r, "streaming", False) else r.content
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertIn("viewer@wm.test", "".join(z.read(n).decode() for n in z.namelist() if n.startswith("word/header")))
        # The owner edits the original.
        r, _ = self.get(self.owner, f"/api/v1/core/onlyoffice/editor-config/?file_id={f.pk}")
        cfg = r.json()["config"]
        self.assertEqual(cfg["editorConfig"]["mode"], "edit")
        url = urlparse(cfg["document"]["url"])
        r = APIClient().get(f"{url.path}?{url.query}")
        data = b"".join(r.streaming_content) if getattr(r, "streaming", False) else r.content
        self.assertEqual(data, path.read_bytes())

    def test_signed_download_tokens_cannot_be_altered(self):
        from django.core.signing import Signer
        from core.onlyoffice_utils import make_signed_download_url, signed_download_claims, verify_signed_download_token
        f, _ = self.document("x.docx", make_docx)
        plain = parse_qs(urlparse(make_signed_download_url(str(f.pk))).query)["token"][0]
        self.assertEqual(signed_download_claims(plain, str(f.pk)), (True, None))
        self.assertTrue(verify_signed_download_token(plain, str(f.pk)))                # old-format tokens still work
        marked = parse_qs(urlparse(make_signed_download_url(str(f.pk), viewer_id=self.viewer.pk)).query)["token"][0]
        self.assertEqual(signed_download_claims(marked, str(f.pk)), (True, self.viewer.pk))
        self.assertFalse(signed_download_claims(marked, str(f.pk + 1))[0])             # bound to the file
        stripped = marked.rsplit(":", 1)[0]                                            # dropping the viewer breaks it
        self.assertFalse(signed_download_claims(stripped, str(f.pk))[0])
        forged = Signer(salt="something-else").sign(f"{f.pk}:9999999999")
        self.assertFalse(signed_download_claims(forged, str(f.pk))[0])

    def test_admin_can_choose_allow_or_block(self):
        admin = make_user("admin@wm.test", self.firm, groups=["Admin"])
        c = APIClient()
        c.force_authenticate(admin)
        self.assertEqual(c.patch("/api/v1/auth/admin/policy/", {"watermark_unsupported": "maybe"}, format="json").status_code, 400)
        r = c.patch("/api/v1/auth/admin/policy/", {"watermark_unsupported": "block"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["watermark_unsupported"], "block")
