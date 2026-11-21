"""
Watermarks on viewed, downloaded and shared copies, when the document owner's organisation turns them on
(Users & access, Settings: watermark_downloads). The stored document is never changed: each copy is stamped as
it is served, so a leaked copy shows who took it and when.

Formats stamped:
  PDF                     diagonal mark and a footer line on every page (PyMuPDF)
  Word  .docx/.docm       Word watermark (diagonal, behind the text) and a header line, in every section
  Excel .xlsx/.xlsm       header and footer on every sheet (shown on screen in Page Layout, and on every print)
  PowerPoint .pptx/.pptm  diagonal mark and a footer line on every slide
  Images .png/.jpg/.jpeg/.webp/.gif/.bmp/.tif/.tiff   diagonal mark and a footer line
Anything else (for example .doc, .xls, .csv, .txt) cannot carry a mark. What happens then is the organisation's
choice (watermark_unsupported): "allow" serves it unmarked and records that, "block" refuses the copy.

Template placeholders: {email} (the signed-in viewer, or "Shared link"), {date}, {org}.
"""
from __future__ import annotations

import io
import logging
import os
import re
import zipfile
from xml.sax.saxutils import escape

from django.utils import timezone
from lxml import etree

logger = logging.getLogger(__name__)
DEFAULT_TEMPLATE = "Confidential · {email} · {date}"
MAX_PAGES = 2000
MAX_OOXML_BYTES = 200 * 1024 * 1024        # uncompressed total; refuses zip bombs
MAX_IMAGE_PIXELS = 80_000_000

WORD = {".docx", ".docm"}
EXCEL = {".xlsx", ".xlsm"}
POWERPOINT = {".pptx", ".pptm"}
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
STAMPABLE = {".pdf"} | WORD | EXCEL | POWERPOINT | IMAGES


class WatermarkRefused(Exception):
    """The organisation requires a watermark and this format cannot carry one (watermark_unsupported=block)."""


def text_for(owner_client_id, viewer=None) -> str | None:
    """The watermark for this copy, or None when the owner's organisation does not watermark."""
    policy = _policy(owner_client_id)
    if not policy or not policy.watermark_downloads:
        return None
    template = (policy.watermark_text or DEFAULT_TEMPLATE)[:200]
    values = {"email": getattr(viewer, "email", None) or "Shared link",
              "date": timezone.localtime().strftime("%Y-%m-%d %H:%M"),
              "org": getattr(policy.client, "name", "")}
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return DEFAULT_TEMPLATE.format(**values)


def _policy(owner_client_id):
    if not owner_client_id:
        return None
    from custom_authentication.models import OrganisationPolicy
    return OrganisationPolicy.objects.filter(client_id=owner_client_id).select_related("client") \
        .only("watermark_downloads", "watermark_text", "watermark_unsupported", "client__name").first()


def blocks_unsupported(owner_client_id) -> bool:
    policy = _policy(owner_client_id)
    return bool(policy and policy.watermark_downloads and policy.watermark_unsupported == "block")


def ext_of(path) -> str:
    return os.path.splitext(str(path))[1].lower()


def is_pdf(path) -> bool:
    return ext_of(path) == ".pdf"


def can_stamp(path) -> bool:
    return ext_of(path) in STAMPABLE


# ── PDF ──────────────────────────────────────────────────────────────────────────────────────
def stamp_pdf(path, text) -> bytes:
    """A watermarked copy of the PDF at `path` (diagonal mark plus a footer line on every page)."""
    import fitz
    with fitz.open(path) as doc:
        if doc.page_count > MAX_PAGES:
            raise ValueError("Too many pages to watermark.")
        for page in doc:
            rect = page.rect
            size = max(18, min(48, rect.width / max(len(text), 1) * 1.6))
            centre = fitz.Point(rect.width / 2, rect.height / 2)
            start = fitz.Point(centre.x - len(text) * size * 0.25, centre.y)
            page.insert_text(start, text, fontsize=size, color=(0.55, 0.55, 0.55), fill_opacity=0.22,
                             morph=(centre, fitz.Matrix(-35)), overlay=True)
            page.insert_text(fitz.Point(24, rect.height - 14), text, fontsize=8, color=(0.4, 0.4, 0.4),
                             fill_opacity=0.8, overlay=True)
        return doc.tobytes(garbage=3, deflate=True)


# ── Office Open XML (shared) ─────────────────────────────────────────────────────────────────
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
HEADER_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header"
HEADER_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
SML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
PML = "http://schemas.openxmlformats.org/presentationml/2006/main"
DML = "http://schemas.openxmlformats.org/drawingml/2006/main"
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False, remove_blank_text=False)


def _xml(data: bytes):
    return etree.fromstring(data, parser=_PARSER)


def _bytes(root) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


class _Package:
    """An OOXML zip read into memory, edited part by part, written back with the original order."""

    def __init__(self, path):
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if sum(i.file_size for i in infos) > MAX_OOXML_BYTES:
                raise ValueError("The document is too large to watermark.")
            self.order = [i.filename for i in infos]
            self.parts = {i.filename: z.read(i) for i in infos}

    def get(self, name):
        return _xml(self.parts[name]) if name in self.parts else None

    def put(self, name, root_or_bytes):
        if name not in self.parts:
            self.order.append(name)
        self.parts[name] = root_or_bytes if isinstance(root_or_bytes, bytes) else _bytes(root_or_bytes)

    def names(self, pattern):
        rx = re.compile(pattern)
        return [n for n in self.order if rx.fullmatch(n)]

    def tobytes(self) -> bytes:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for name in self.order:
                z.writestr(name, self.parts[name])
        return out.getvalue()


def _rels_name(part):
    folder, base = os.path.split(part)
    return f"{folder}/_rels/{base}.rels" if folder else f"_rels/{base}.rels"


def _resolve(part, target):
    if target.startswith("/"):
        return target.lstrip("/")
    return os.path.normpath(os.path.join(os.path.dirname(part), target)).replace("\\", "/")


# ── Word ─────────────────────────────────────────────────────────────────────────────────────
_SHAPETYPE = (
    '<v:shapetype id="_x0000_t136" coordsize="21600,21600" o:spt="136" adj="10800" path="m@7,l@8,m@5,21600l@6,21600e">'
    '<v:formulas><v:f eqn="sum #0 0 10800"/><v:f eqn="prod #0 2 1"/><v:f eqn="sum 21600 0 @1"/><v:f eqn="sum 0 0 @2"/>'
    '<v:f eqn="sum 21600 0 @3"/><v:f eqn="if @0 @3 0"/><v:f eqn="if @0 21600 @1"/><v:f eqn="if @0 0 @2"/>'
    '<v:f eqn="if @0 @4 21600"/><v:f eqn="mid @5 @6"/><v:f eqn="mid @8 @5"/><v:f eqn="mid @7 @8"/><v:f eqn="mid @6 @7"/>'
    '<v:f eqn="sum @6 0 @5"/></v:formulas><v:path textpathok="t" o:connecttype="custom" '
    'o:connectlocs="@9,0;@10,10800;@11,21600;@12,10800" o:connectangles="270,180,90,0"/>'
    '<v:textpath on="t" fitshape="t"/><v:handles><v:h position="#0,bottomRight" xrange="6629,14971"/></v:handles>'
    '<o:lock v:ext="edit" text="t" shapetype="t"/></v:shapetype>')


def _word_paragraphs(text) -> list:
    """Two header paragraphs: the classic Word watermark shape, and a small visible line (every viewer shows it)."""
    t = escape(text, {'"': "&quot;"})
    ns = (f'xmlns:w="{W}" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office" '
          f'xmlns:w10="urn:schemas-microsoft-com:office:word"')
    shape = (f'<w:p {ns}><w:pPr><w:spacing w:after="0"/></w:pPr><w:r><w:rPr><w:noProof/></w:rPr><w:pict>{_SHAPETYPE}'
             f'<v:shape id="AiDocuMinesWatermark" o:spid="_x0000_s9001" type="#_x0000_t136" '
             f'style="position:absolute;margin-left:0;margin-top:0;width:468pt;height:78pt;rotation:315;z-index:-251654144;'
             f'mso-position-horizontal:center;mso-position-horizontal-relative:margin;mso-position-vertical:center;'
             f'mso-position-vertical-relative:margin" o:allowincell="f" fillcolor="#a6a6a6" stroked="f">'
             f'<v:fill opacity=".35"/><v:textpath style="font-family:&quot;Calibri&quot;;font-size:1pt" string="{t}"/>'
             f'<w10:wrap anchorx="margin" anchory="margin"/></v:shape></w:pict></w:r></w:p>')
    line = (f'<w:p {ns}><w:pPr><w:jc w:val="center"/><w:spacing w:after="0"/></w:pPr><w:r><w:rPr><w:color w:val="808080"/>'
            f'<w:sz w:val="16"/></w:rPr><w:t xml:space="preserve">{t}</w:t></w:r></w:p>')
    return [etree.fromstring(shape), etree.fromstring(line)]


def stamp_docx(path, text) -> bytes:
    pkg = _Package(path)
    doc_name = "word/document.xml"
    document = pkg.get(doc_name)
    if document is None:
        raise ValueError("Not a Word document.")
    rels_name = _rels_name(doc_name)
    rels = pkg.get(rels_name)
    if rels is None:
        rels = etree.fromstring(f'<Relationships xmlns="{PKG_REL}"/>')
    targets = {r.get("Id"): _resolve(doc_name, r.get("Target")) for r in rels if r.get("Type") == HEADER_REL}

    stamped, new_header_rid = set(), None
    sections = document.findall(f".//{{{W}}}sectPr")
    for index, sect in enumerate(sections):
        refs = {ref.get(f"{{{W}}}type", "default"): ref.get(f"{{{R}}}id") for ref in sect.findall(f"{{{W}}}headerReference")}
        for rid in refs.values():                           # every existing header: add the mark once
            part = targets.get(rid)
            if part and part not in stamped and part in pkg.parts:
                header = pkg.get(part)
                header.extend(_word_paragraphs(text))
                pkg.put(part, header)
                stamped.add(part)
        if index == 0:
            # Later sections without their own headers inherit; only the first needs one of each kind.
            for kind in ("default", "first", "even"):
                if kind in refs:
                    continue
                if new_header_rid is None:
                    new_header_rid = _add_word_header(pkg, rels, text)
                ref = etree.Element(f"{{{W}}}headerReference", nsmap={"w": W, "r": R})
                ref.set(f"{{{W}}}type", kind)
                ref.set(f"{{{R}}}id", new_header_rid)
                sect.insert(0, ref)                          # header references come first in sectPr
    if not sections:
        raise ValueError("The Word document has no section properties.")
    pkg.put(doc_name, document)
    pkg.put(rels_name, rels)
    return pkg.tobytes()


def _add_word_header(pkg, rels, text) -> str:
    n = 1
    while f"word/header_aidoc{n}.xml" in pkg.parts:
        n += 1
    part = f"word/header_aidoc{n}.xml"
    header = etree.Element(f"{{{W}}}hdr", nsmap={"w": W, "r": R})
    header.extend(_word_paragraphs(text))
    pkg.put(part, header)
    used = {r.get("Id") for r in rels}
    rid = next(f"rIdAidocWm{i}" for i in range(1, 1000) if f"rIdAidocWm{i}" not in used)
    rel = etree.SubElement(rels, f"{{{PKG_REL}}}Relationship")
    rel.set("Id", rid)
    rel.set("Type", HEADER_REL)
    rel.set("Target", os.path.basename(part))
    _content_type(pkg, "/" + part, HEADER_CT)
    return rid


def _content_type(pkg, part_name, content_type):
    types = pkg.get("[Content_Types].xml")
    if not any(o.get("PartName") == part_name for o in types.findall(f"{{{CT}}}Override")):
        override = etree.SubElement(types, f"{{{CT}}}Override")
        override.set("PartName", part_name)
        override.set("ContentType", content_type)
        pkg.put("[Content_Types].xml", types)


# ── Excel ────────────────────────────────────────────────────────────────────────────────────
# Elements that must come after <headerFooter> in a worksheet (CT_Worksheet sequence).
_AFTER_HEADER_FOOTER = ["rowBreaks", "colBreaks", "customProperties", "cellWatches", "ignoredErrors", "smartTags",
                        "drawing", "legacyDrawing", "legacyDrawingHF", "drawingHF", "picture", "oleObjects", "controls",
                        "webPublishItems", "tableParts", "extLst"]


def _with_centre(existing: str, centre: str) -> str:
    """Replace the centre section (&C...) of an Excel header/footer string, keeping left and right."""
    kept = re.sub(r"&C.*?(?=&L|&R|$)", "", existing or "", flags=re.S)
    return kept + "&C" + centre


def stamp_xlsx(path, text) -> bytes:
    pkg = _Package(path)
    sheets = pkg.names(r"xl/worksheets/[^/]+\.xml")
    if not sheets:
        raise ValueError("Not an Excel workbook.")
    centre = '&"Calibri,Bold"&12&K808080' + text.replace("&", "&&")
    for name in sheets:
        sheet = pkg.get(name)
        hf = sheet.find(f"{{{SML}}}headerFooter")
        if hf is None:
            hf = etree.Element(f"{{{SML}}}headerFooter")
            following = next((sheet.find(f"{{{SML}}}{tag}") for tag in _AFTER_HEADER_FOOTER
                              if sheet.find(f"{{{SML}}}{tag}") is not None), None)
            if following is not None:
                following.addprevious(hf)
            else:
                sheet.append(hf)
        kinds = ["oddHeader", "oddFooter"]
        if hf.get("differentOddEven") in ("1", "true"):
            kinds += ["evenHeader", "evenFooter"]
        if hf.get("differentFirst") in ("1", "true"):
            kinds += ["firstHeader", "firstFooter"]
        order = ["oddHeader", "oddFooter", "evenHeader", "evenFooter", "firstHeader", "firstFooter"]
        for kind in kinds:
            el = hf.find(f"{{{SML}}}{kind}")
            if el is None:
                el = etree.Element(f"{{{SML}}}{kind}")
                later = next((hf.find(f"{{{SML}}}{k}") for k in order[order.index(kind) + 1:]
                              if hf.find(f"{{{SML}}}{k}") is not None), None)
                if later is not None:
                    later.addprevious(el)
                else:
                    hf.append(el)
            el.text = _with_centre(el.text, centre)[:255]
        pkg.put(name, sheet)
    return pkg.tobytes()


# ── PowerPoint ───────────────────────────────────────────────────────────────────────────────
def stamp_pptx(path, text) -> bytes:
    pkg = _Package(path)
    slides = pkg.names(r"ppt/slides/slide\d+\.xml")
    if not slides:
        raise ValueError("Not a PowerPoint presentation.")
    pres = pkg.get("ppt/presentation.xml")
    size = pres.find(f"{{{PML}}}sldSz") if pres is not None else None
    cx, cy = (int(size.get("cx")), int(size.get("cy"))) if size is not None else (12192000, 6858000)
    t = escape(text)
    font = max(1800, min(4800, int(cx / max(len(text), 1) / 127 * 1.4)))    # hundredths of a point
    for name in slides:
        slide = pkg.get(name)
        tree = slide.find(f"{{{PML}}}cSld/{{{PML}}}spTree")
        if tree is None:
            continue
        ids = [int(e.get("id")) for e in slide.iter(f"{{{PML}}}cNvPr") if str(e.get("id", "")).isdigit()]
        next_id = max(ids, default=1) + 1
        w, h = int(cx * 0.9), int(cy * 0.22)
        tree.append(_pptx_box(next_id, "Watermark", t, (cx - w) // 2, (cy - h) // 2, w, h, font, 30000, -2700000))
        tree.append(_pptx_box(next_id + 1, "Watermark line", t, 0, cy - int(cy * 0.06), cx, int(cy * 0.05), 900, 80000, 0))
        pkg.put(name, slide)
    return pkg.tobytes()


def _pptx_box(shape_id, name, text, x, y, w, h, size, alpha, rot):
    rot_attr = f' rot="{rot}"' if rot else ""
    return etree.fromstring(
        f'<p:sp xmlns:p="{PML}" xmlns:a="{DML}"><p:nvSpPr><p:cNvPr id="{shape_id}" name="{name}"/>'
        f'<p:cNvSpPr txBox="1"/><p:nvPr userDrawn="1"/></p:nvSpPr>'
        f'<p:spPr><a:xfrm{rot_attr}><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
        f'<p:txBody><a:bodyPr wrap="none" anchor="ctr" rtlCol="0"><a:normAutofit/></a:bodyPr><a:lstStyle/>'
        f'<a:p><a:pPr algn="ctr"/><a:r><a:rPr lang="en-US" sz="{size}" b="1" dirty="0">'
        f'<a:solidFill><a:srgbClr val="808080"><a:alpha val="{alpha}"/></a:srgbClr></a:solidFill></a:rPr>'
        f'<a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>')


# ── Images ───────────────────────────────────────────────────────────────────────────────────
def stamp_image(path, text) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    with Image.open(path) as source:
        fmt = source.format or "PNG"
        frame = source.convert("RGBA")
    width, height = frame.size
    size = max(14, min(96, int(width / max(len(text), 1) * 1.5)))
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:                                      # older Pillow
        font = ImageFont.load_default()
    layer = Image.new("RGBA", (width * 2, height * 2), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((width, height), text, font=font, fill=(128, 128, 128, 80), anchor="mm")
    layer = layer.rotate(35, resample=Image.BICUBIC).crop((width // 2, height // 2, width // 2 + width, height // 2 + height))
    marked = Image.alpha_composite(frame, layer)
    small = ImageFont.load_default(size=max(10, size // 4)) if size >= 40 else font
    ImageDraw.Draw(marked).text((8, height - 8), text, font=small, fill=(90, 90, 90, 200), anchor="ls")
    out = io.BytesIO()
    if fmt == "JPEG":
        marked.convert("RGB").save(out, format="JPEG", quality=92)
    elif fmt == "BMP":
        marked.convert("RGB").save(out, format="BMP")
    elif fmt == "GIF":
        marked.convert("P", palette=Image.ADAPTIVE).save(out, format="GIF")
    else:
        marked.save(out, format=fmt if fmt in ("PNG", "WEBP", "TIFF") else "PNG")
    return out.getvalue()


# ── entry points ─────────────────────────────────────────────────────────────────────────────
def stamp(path, text) -> bytes:
    ext = ext_of(path)
    if ext == ".pdf":
        return stamp_pdf(path, text)
    if ext in WORD:
        return stamp_docx(path, text)
    if ext in EXCEL:
        return stamp_xlsx(path, text)
    if ext in POWERPOINT:
        return stamp_pptx(path, text)
    if ext in IMAGES:
        return stamp_image(path, text)
    raise ValueError(f"{ext or 'This'} files cannot carry a watermark.")


def watermarked_or_none(path, text, *, owner_client_id=None, name=None):
    """Bytes of a stamped copy, or None when no mark is required or the format cannot carry one (and the
    organisation allows unmarked copies). Raises WatermarkRefused when the organisation blocks those copies.
    A file that should be stamped but fails to stamp is never served unmarked: the error propagates."""
    if not text or not os.path.isfile(path):
        return None
    if not can_stamp(name or path):
        if blocks_unsupported(owner_client_id):
            raise WatermarkRefused(f"Your organisation requires watermarks, and {ext_of(name or path) or 'this'} files "
                                   "cannot carry one. Ask the owner for a PDF or Office copy.")
        logger.info("Served an unwatermarked %s copy (format cannot carry a mark)", ext_of(name or path))
        return None
    try:
        return stamp(path, text) if can_stamp(path) else _stamp_as(path, name, text)
    except Exception:
        logger.exception("Could not watermark %s", path)
        raise


def _stamp_as(path, name, text):
    """Stored files may lack an extension; stamp by the display name's type."""
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "document" + ext_of(name))
        shutil.copyfile(path, copy)
        return stamp(copy, text)


def content_type_for(name) -> str:
    import mimetypes
    return mimetypes.guess_type(str(name))[0] or "application/octet-stream"
