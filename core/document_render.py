"""
Show any stored document in the browser, safely.

The browser can display only a few types itself (see core/file_responses.py). Everything else is
turned into something it can show without running anything from the file:

  kind      what the viewer receives
  ------    ---------------------------------------------------------------------------------
  pdf       a PDF (converted: Office via OnlyOffice or LibreOffice, SVG/TIFF/EPUB/XPS via PyMuPDF)
  image     a PNG (converted with Pillow) or a browser-safe image
  media     audio or video the browser plays itself, with a fixed content type
  text      plain text (code, data, logs, any file that decodes as text)
  html      HTML (web pages, Markdown, Word/RTF fallback), shown in a sandboxed frame with no scripts
  table     sheets of rows (CSV/TSV, Excel, OpenDocument spreadsheets)
  slides    the text of each slide (PowerPoint, when it cannot be converted to PDF)
  email     headers, body and attachment names (.eml, .msg)
  archive   the list of entries (zip, tar)
  details   facts about the file, for formats with no visual form (with any readable text found)

render_manifest() returns the JSON description; render_content() returns the converted binary for
pdf/image/media. Conversions are cached under MEDIA_ROOT/previews, keyed by file id, size and mtime.
"""
from __future__ import annotations

import csv
import email
import email.policy
import hashlib
import io
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile

from django.conf import settings

logger = logging.getLogger(__name__)

MAX_TEXT_BYTES = 2 * 1024 * 1024        # text shown in full up to 2 MB
MAX_TABLE_ROWS = 2000                   # per sheet (the viewer pages through them)
MAX_TABLE_COLS = 200
MAX_SHEETS = 20
MAX_ARCHIVE_ENTRIES = 5000
CONVERT_TIMEOUT = 120                   # seconds, per conversion

# Browser-native types: served as they are (kind, content type).
NATIVE = {
    ".pdf": ("pdf", "application/pdf"),
    ".png": ("image", "image/png"), ".jpg": ("image", "image/jpeg"), ".jpeg": ("image", "image/jpeg"),
    ".gif": ("image", "image/gif"), ".webp": ("image", "image/webp"), ".bmp": ("image", "image/bmp"),
    ".avif": ("image", "image/avif"),
    ".mp3": ("media", "audio/mpeg"), ".wav": ("media", "audio/wav"), ".ogg": ("media", "audio/ogg"),
    ".oga": ("media", "audio/ogg"), ".m4a": ("media", "audio/mp4"), ".aac": ("media", "audio/aac"),
    ".flac": ("media", "audio/flac"), ".opus": ("media", "audio/ogg"),
    ".mp4": ("media", "video/mp4"), ".m4v": ("media", "video/mp4"), ".webm": ("media", "video/webm"),
    ".ogv": ("media", "video/ogg"), ".mov": ("media", "video/quicktime"),
}
OFFICE_DOCS = {".docx", ".doc", ".docm", ".dotx", ".dot", ".odt", ".ott", ".rtf", ".wpd", ".wps", ".pages"}
OFFICE_SHEETS = {".xlsx", ".xls", ".xlsm", ".xltx", ".xlsb", ".ods", ".ots"}
OFFICE_SLIDES = {".pptx", ".ppt", ".pptm", ".potx", ".pps", ".ppsx", ".odp", ".otp"}
DELIMITED = {".csv": ",", ".tsv": "\t", ".tab": "\t"}
PYMUPDF_TO_PDF = {".svg", ".tif", ".tiff", ".xps", ".oxps", ".epub", ".mobi", ".fb2", ".cbz", ".jxr",
                  ".jpx", ".jp2", ".pnm", ".pgm", ".ppm", ".pbm", ".pam"}
PILLOW_TO_PNG = {".ico", ".tga", ".psd", ".pcx", ".dds", ".icns", ".heic", ".heif", ".jfif", ".dib", ".xbm"}
HTML_EXT = {".html", ".htm", ".xhtml", ".mht", ".mhtml"}
MARKDOWN_EXT = {".md", ".markdown", ".mdown", ".mkd"}
EMAIL_EXT = {".eml", ".msg"}
ARCHIVE_EXT = {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".tbz", ".txz"}
LANGUAGE = {".py": "python", ".js": "javascript", ".ts": "typescript", ".json": "json", ".xml": "xml",
            ".yaml": "yaml", ".yml": "yaml", ".sql": "sql", ".sh": "shell", ".css": "css",
            ".java": "java", ".cs": "csharp", ".go": "go", ".rb": "ruby", ".php": "php", ".c": "c",
            ".cpp": "cpp", ".h": "c", ".rs": "rust", ".kt": "kotlin", ".swift": "swift", ".r": "r",
            ".ini": "ini", ".toml": "toml", ".log": "log", ".tex": "latex"}

KIND_NAMES = {"pdf": "PDF", "image": "image", "media": "media"}


# ───────────────────────────── helpers ─────────────────────────────

def _ext(name: str) -> str:
    return os.path.splitext(str(name or ""))[1].lower()


def _cache_dir() -> str:
    d = os.path.join(settings.MEDIA_ROOT, "previews")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path(file_obj, path: str, suffix: str) -> str:
    st = os.stat(path)
    key = hashlib.sha256(f"{file_obj.pk}:{st.st_size}:{int(st.st_mtime)}".encode()).hexdigest()[:24]
    return os.path.join(_cache_dir(), f"{file_obj.pk}-{key}{suffix}")


def _read_text(path: str, limit: int = MAX_TEXT_BYTES) -> tuple[str, bool] | None:
    """Decode a file as text, or None when it is binary."""
    with open(path, "rb") as fh:
        raw = fh.read(limit + 1)
    truncated = len(raw) > limit
    raw = raw[:limit]
    if not raw:
        return "", False
    if b"\x00" in raw[:8192] and not (raw[:2] in (b"\xff\xfe", b"\xfe\xff")):
        return None
    for enc in ("utf-8-sig", "utf-16"):
        if enc == "utf-16" and raw[:2] not in (b"\xff\xfe", b"\xfe\xff"):
            continue
        try:
            return raw.decode(enc), truncated
        except UnicodeDecodeError:
            pass
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(raw).best()
        if best is not None and best.encoding:
            text = str(best)
            printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in text[:4096])
            if printable >= 0.95 * min(len(text), 4096):
                return text, truncated
    except Exception:
        pass
    return None


_SCRIPT_RE = re.compile(r"<\s*(script|iframe|object|embed|frame|frameset|base|meta|link)\b[^>]*>.*?<\s*/\s*\1\s*>|"
                        r"<\s*(script|iframe|object|embed|frame|frameset|base|meta|link)\b[^>]*/?>",
                        re.I | re.S)
_ON_ATTR_RE = re.compile(r"\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_JS_URL_RE = re.compile(r"(href|src|action|formaction|xlink:href)\s*=\s*([\"']?)\s*javascript:[^\"'>\s]*\2", re.I)


def _defang_html(html: str) -> str:
    """Remove scripts, event handlers and active elements. The viewer also shows HTML in a sandboxed
    frame with scripts disabled and a no-network policy; this is the second line of defence."""
    html = _SCRIPT_RE.sub("", html)
    html = _ON_ATTR_RE.sub("", html)
    return _JS_URL_RE.sub(r'\1=""', html)


def _facts(file_obj, path: str, kind_label: str | None = None) -> list[dict]:
    st = os.stat(path)
    name = getattr(file_obj, "filename", None) or os.path.basename(path)
    guessed = mimetypes.guess_type(name)[0]
    facts = [
        {"label": "Name", "value": name},
        {"label": "Type", "value": kind_label or (_ext(name).lstrip(".").upper() or "Unknown") + " file"},
        {"label": "Size", "value": _human_size(st.st_size)},
        {"label": "Modified", "value": time.strftime("%d %b %Y, %H:%M", time.localtime(st.st_mtime))},
    ]
    if guessed:
        facts.append({"label": "Format", "value": guessed})
    return facts


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _strings(path: str, limit: int = 200_000) -> str:
    """Readable runs of text inside a binary file (like the Unix `strings` tool)."""
    with open(path, "rb") as fh:
        raw = fh.read(4 * 1024 * 1024)
    runs = re.findall(rb"[\x20-\x7e\t]{6,}", raw)
    text = "\n".join(r.decode("ascii", "ignore") for r in runs)
    return text[:limit]


# ───────────────────────────── conversions to PDF / PNG ─────────────────────────────

def _onlyoffice_to_pdf(file_obj, out_path: str) -> bool:
    conf = getattr(settings, "ONLYOFFICE", {}) or {}
    base, secret = (conf.get("DOC_SERVER_URL") or "").rstrip("/"), conf.get("JWT_SECRET") or ""
    if not base or not secret:
        return False
    try:
        import jwt
        import requests
        from core.onlyoffice_utils import ext_from_filename, make_signed_download_url
        body = {"async": False, "filetype": ext_from_filename(file_obj.filename), "outputtype": "pdf",
                "key": f"preview-{file_obj.pk}-{int(os.path.getmtime(file_obj.filepath))}",
                "title": file_obj.filename, "url": make_signed_download_url(str(file_obj.pk))}
        signed = dict(body, token=jwt.encode(body, secret, algorithm="HS256"))
        connect = int(conf.get("HTTP_CONNECT_TIMEOUT", 5))
        resp = requests.post(f"{base}/ConvertService.ashx", json=signed, timeout=(connect, CONVERT_TIMEOUT),
                             headers={"Accept": "application/json"})
        data = resp.json() or {}
        url = data.get("fileUrl")
        if not (url and data.get("endConvert")):
            logger.info("OnlyOffice preview conversion declined for file %s: %s", file_obj.pk, data.get("error"))
            return False
        with requests.get(url, timeout=(connect, CONVERT_TIMEOUT), stream=True) as r:
            r.raise_for_status()
            with open(out_path + ".part", "wb") as fh:
                for chunk in r.iter_content(1024 * 256):
                    fh.write(chunk)
        os.replace(out_path + ".part", out_path)
        return True
    except Exception as exc:
        logger.info("OnlyOffice preview conversion failed for file %s: %s", file_obj.pk, exc)
        return False


def _libreoffice_to_pdf(path: str, out_path: str) -> bool:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "source" + _ext(path))
        shutil.copyfile(path, src)
        try:
            subprocess.run([soffice, f"-env:UserInstallation=file://{tmp}/profile", "--headless", "--norestore",
                            "--convert-to", "pdf", "--outdir", tmp, src],
                           check=True, timeout=CONVERT_TIMEOUT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            logger.info("LibreOffice preview conversion failed for %s: %s", os.path.basename(path), exc)
            return False
        produced = os.path.join(tmp, "source.pdf")
        if not os.path.exists(produced) or os.path.getsize(produced) == 0:
            return False
        shutil.move(produced, out_path)
        return True


def _pymupdf_to_pdf(path: str, out_path: str) -> bool:
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        with fitz.open(path) as doc:
            data = doc.convert_to_pdf()
        with open(out_path, "wb") as fh:
            fh.write(data)
        return True
    except Exception as exc:
        logger.info("PyMuPDF preview conversion failed for %s: %s", os.path.basename(path), exc)
        return False


def _pillow_to_png(path: str, out_path: str) -> bool:
    try:
        from PIL import Image
        try:
            import pillow_heif  # optional: HEIC/HEIF
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        with Image.open(path) as im:
            im.seek(0)
            im = im.convert("RGBA") if im.mode not in ("RGB", "RGBA", "L", "LA") else im
            im.save(out_path, "PNG")
        return True
    except Exception as exc:
        logger.info("Pillow preview conversion failed for %s: %s", os.path.basename(path), exc)
        return False


def _converted(file_obj, path: str, ext: str) -> tuple[str, str, str] | None:
    """Path, content type and kind of a converted rendition, creating it if needed."""
    if ext in PILLOW_TO_PNG:
        out = _cache_path(file_obj, path, ".png")
        if os.path.exists(out) or _pillow_to_png(path, out):
            return out, "image/png", "image"
        return None
    out = _cache_path(file_obj, path, ".pdf")
    if os.path.exists(out):
        return out, "application/pdf", "pdf"
    failed = out + ".failed"
    if os.path.exists(failed) and time.time() - os.path.getmtime(failed) < 3600:
        return None                      # tried within the hour: use the structured fallback
    if ext in PYMUPDF_TO_PDF:
        ok = _pymupdf_to_pdf(path, out)
    elif ext in OFFICE_DOCS | OFFICE_SHEETS | OFFICE_SLIDES:
        ok = _onlyoffice_to_pdf(file_obj, out) or _libreoffice_to_pdf(path, out)
    else:
        ok = False
    if not ok:
        try:
            open(failed, "w").close()
        except OSError:
            pass
        return None
    return out, "application/pdf", "pdf"


# ───────────────────────────── structured renderings ─────────────────────────────

def _table_from_delimited(path: str, delimiter: str) -> dict:
    decoded = _read_text(path, limit=20 * 1024 * 1024)
    text = decoded[0] if decoded else ""
    try:
        dialect = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|")
        delimiter = dialect.delimiter
    except Exception:
        pass
    rows, truncated = [], False
    for i, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter)):
        if i > MAX_TABLE_ROWS:
            truncated = True
            break
        rows.append(row[:MAX_TABLE_COLS])
    return {"kind": "table", "sheets": [_sheet("Sheet 1", rows, truncated)]}


def _sheet(name: str, rows: list[list], truncated: bool) -> dict:
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    while rows and not any(c.strip() for c in rows[-1]):
        rows.pop()
    width = max((len(r) for r in rows), default=0)
    header = rows[0] if rows else []
    looks_like_header = bool(header) and all(c.strip() for c in header) and len(set(header)) == len(header)
    if looks_like_header:
        columns, body = header + [f"Column {i + 1}" for i in range(len(header), width)], rows[1:]
    else:
        columns, body = [_col_letter(i) for i in range(width)], rows
    body = [r + [""] * (width - len(r)) for r in body]
    return {"name": name, "columns": columns, "rows": body, "truncated": truncated}


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _table_from_workbook(path: str, ext: str) -> dict | None:
    sheets = []
    try:
        if ext in (".xlsx", ".xlsm", ".xltx"):
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            for ws in wb.worksheets[:MAX_SHEETS]:
                rows, truncated = [], False
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i > MAX_TABLE_ROWS:
                        truncated = True
                        break
                    rows.append(list(row[:MAX_TABLE_COLS]))
                sheets.append(_sheet(ws.title, rows, truncated))
            wb.close()
        elif ext == ".xls":
            import xlrd
            wb = xlrd.open_workbook(path, on_demand=True)
            for ws in wb.sheets()[:MAX_SHEETS]:
                n = min(ws.nrows, MAX_TABLE_ROWS + 1)
                rows = [ws.row_values(i)[:MAX_TABLE_COLS] for i in range(n)]
                sheets.append(_sheet(ws.name, rows, ws.nrows > n))
        else:
            return None
    except Exception as exc:
        logger.info("Spreadsheet preview failed for %s: %s", os.path.basename(path), exc)
        return None
    return {"kind": "table", "sheets": sheets} if sheets else None


def _slides_from_pptx(path: str) -> dict | None:
    try:
        from pptx import Presentation
        prs = Presentation(path)
        slides = []
        for idx, slide in enumerate(prs.slides, 1):
            title, lines = "", []
            for shape in slide.shapes:
                if not getattr(shape, "has_text_frame", False):
                    continue
                text = "\n".join(p.text for p in shape.text_frame.paragraphs if p.text.strip())
                if not text:
                    continue
                if not title and shape == getattr(slide.shapes, "title", None):
                    title = text
                else:
                    lines.append(text)
            slides.append({"number": idx, "title": title or f"Slide {idx}", "text": "\n\n".join(lines)})
        return {"kind": "slides", "slides": slides}
    except Exception as exc:
        logger.info("Slides preview failed for %s: %s", os.path.basename(path), exc)
        return None


def _html_from_word(path: str, ext: str) -> dict | None:
    try:
        if ext == ".docx":
            import docx
            d = docx.Document(path)
            parts = []
            for p in d.paragraphs:
                if not p.text.strip():
                    continue
                style = (p.style.name or "").lower() if p.style is not None else ""
                tag = "h2" if style.startswith("heading") or style == "title" else "p"
                parts.append(f"<{tag}>{_esc(p.text)}</{tag}>")
            for t in d.tables:
                rows = "".join("<tr>" + "".join(f"<td>{_esc(c.text)}</td>" for c in r.cells) + "</tr>" for r in t.rows)
                parts.append(f"<table>{rows}</table>")
            return {"kind": "html", "html": "\n".join(parts)}
        if ext == ".rtf":
            from striprtf.striprtf import rtf_to_text
            decoded = _read_text(path)
            return {"kind": "text", "text": rtf_to_text(decoded[0] if decoded else ""), "truncated": False}
    except Exception as exc:
        logger.info("Word preview failed for %s: %s", os.path.basename(path), exc)
    return None


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _email(path: str, ext: str) -> dict | None:
    try:
        if ext == ".msg":
            import extract_msg
            m = extract_msg.Message(path)
            try:
                html = m.htmlBody
                if isinstance(html, bytes):
                    html = html.decode("utf-8", "replace")
                return {"kind": "email",
                        "headers": {"From": m.sender or "", "To": m.to or "", "Cc": m.cc or "",
                                    "Date": str(m.date or ""), "Subject": m.subject or ""},
                        "html": _defang_html(html) if html else None,
                        "text": None if html else (m.body or ""),
                        "attachments": [{"name": a.longFilename or a.shortFilename or "attachment",
                                         "size": _human_size(len(a.data or b""))} for a in m.attachments]}
            finally:
                m.close()
        with open(path, "rb") as fh:
            msg = email.message_from_binary_file(fh, policy=email.policy.default)
        html_part = msg.get_body(preferencelist=("html",))
        text_part = msg.get_body(preferencelist=("plain",))
        attachments = []
        for part in msg.iter_attachments():
            payload = part.get_payload(decode=True) or b""
            attachments.append({"name": part.get_filename() or "attachment", "size": _human_size(len(payload))})
        return {"kind": "email",
                "headers": {k: str(msg.get(k, "")) for k in ("From", "To", "Cc", "Date", "Subject")},
                "html": _defang_html(html_part.get_content()) if html_part is not None else None,
                "text": text_part.get_content() if (text_part is not None and html_part is None) else None,
                "attachments": attachments}
    except Exception as exc:
        logger.info("Email preview failed for %s: %s", os.path.basename(path), exc)
        return None


def _archive(path: str, ext: str) -> dict | None:
    entries, truncated = [], False
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                for i, info in enumerate(z.infolist()):
                    if i >= MAX_ARCHIVE_ENTRIES:
                        truncated = True
                        break
                    entries.append({"name": info.filename, "size": "" if info.is_dir() else _human_size(info.file_size),
                                    "modified": "%04d-%02d-%02d %02d:%02d" % info.date_time[:5]})
        elif tarfile.is_tarfile(path):
            with tarfile.open(path) as t:
                for i, m in enumerate(t):
                    if i >= MAX_ARCHIVE_ENTRIES:
                        truncated = True
                        break
                    entries.append({"name": m.name + ("/" if m.isdir() else ""),
                                    "size": "" if m.isdir() else _human_size(m.size),
                                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(m.mtime))})
        elif ext == ".gz":
            import gzip
            with gzip.open(path, "rb") as g:
                inner = g.read(MAX_TEXT_BYTES + 1)
            name = os.path.basename(path)[:-3] or "content"
            try:
                text = inner[:MAX_TEXT_BYTES].decode("utf-8")
                return {"kind": "text", "text": text, "truncated": len(inner) > MAX_TEXT_BYTES, "language": ""}
            except UnicodeDecodeError:
                entries.append({"name": name, "size": "", "modified": ""})
        else:
            return None
    except Exception as exc:
        logger.info("Archive preview failed for %s: %s", os.path.basename(path), exc)
        return None
    return {"kind": "archive", "entries": entries, "truncated": truncated}


def _markdown(text: str) -> str:
    try:
        import markdown
        return markdown.markdown(text, extensions=["tables", "fenced_code"])
    except Exception:
        return f"<pre>{_esc(text)}</pre>"


# ───────────────────────────── public API ─────────────────────────────

def render_manifest(file_obj, path: str) -> dict:
    """Describe how to show this file. Never raises for unreadable formats: falls back to details."""
    name = getattr(file_obj, "filename", None) or os.path.basename(path)
    ext = _ext(name) or _ext(path)
    base = {"name": name, "extension": ext.lstrip(".")}

    if ext in NATIVE:
        kind, ctype = NATIVE[ext]
        return {**base, "kind": kind, "content_type": ctype, "content": True}

    if ext in DELIMITED:
        return {**base, **_table_from_delimited(path, DELIMITED[ext])}

    if ext in OFFICE_DOCS | OFFICE_SHEETS | OFFICE_SLIDES | PYMUPDF_TO_PDF | PILLOW_TO_PNG:
        conv = _converted(file_obj, path, ext)
        if conv:
            return {**base, "kind": conv[2], "content_type": conv[1], "content": True}
        fallback = None
        if ext in OFFICE_SHEETS:
            fallback = _table_from_workbook(path, ext)
        elif ext in OFFICE_SLIDES and ext in (".pptx", ".pptm", ".potx", ".ppsx"):
            fallback = _slides_from_pptx(path)
        elif ext in OFFICE_DOCS:
            fallback = _html_from_word(path, ext)
        elif ext == ".svg":
            decoded = _read_text(path)
            if decoded:
                fallback = {"kind": "text", "text": decoded[0], "truncated": decoded[1], "language": "xml"}
        if fallback:
            return {**base, **fallback}

    if ext in EMAIL_EXT:
        out = _email(path, ext)
        if out:
            return {**base, **out}

    if ext in ARCHIVE_EXT:
        out = _archive(path, ext)
        if out:
            return {**base, **out}

    decoded = _read_text(path)
    if decoded is not None:
        text, truncated = decoded
        if ext in HTML_EXT:
            return {**base, "kind": "html", "html": _defang_html(text), "truncated": truncated}
        if ext in MARKDOWN_EXT:
            return {**base, "kind": "html", "html": _defang_html(_markdown(text)), "truncated": truncated}
        return {**base, "kind": "text", "text": text, "truncated": truncated, "language": LANGUAGE.get(ext, "")}

    # Formats with no visual form (or unknown binaries): show what can be known about them.
    if zipfile.is_zipfile(path):
        out = _archive(path, ".zip")
        if out:
            return {**base, **out}
    found = _strings(path)
    return {**base, "kind": "details", "facts": _facts(file_obj, path),
            "text": found if len(found) > 40 else ""}


def render_content(file_obj, path: str) -> tuple[str, str] | None:
    """(path, content type) of the binary to show for pdf/image/media kinds, or None."""
    name = getattr(file_obj, "filename", None) or os.path.basename(path)
    ext = _ext(name) or _ext(path)
    if ext in NATIVE:
        return path, NATIVE[ext][1]
    conv = _converted(file_obj, path, ext)
    return (conv[0], conv[1]) if conv else None
