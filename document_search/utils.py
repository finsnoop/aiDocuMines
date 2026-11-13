from __future__ import annotations

import json
import logging
import os
import csv
import io
from pathlib import Path
from functools import lru_cache
from typing import List, Tuple, Optional

LOGGER = logging.getLogger(__name__)

# ─────────── Optional dependencies ──────────────────────────
try:
    import fitz
except ImportError:
    fitz = None

try:
    from docx import Document
except ImportError:
    Document = None

try:
    import openpyxl
except ImportError:
    openpyxl = None

try:
    import xlrd
except ImportError:
    xlrd = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    from PIL import Image
    import pytesseract
except ImportError:
    Image = None
    pytesseract = None

try:
    import pandas as pd
except ImportError:
    pd = None

try:
    import nltk
    from nltk.tokenize import sent_tokenize
    nltk.download("punkt", quiet=True)
except ImportError:
    nltk = None

    def sent_tokenize(text):
        import re
        return re.split(r'(?<=[.!?])\s+', text)


# ──────────────── Config ────────────────────────────────────
try:
    from document_search import config
    MODEL_NAME = getattr(config, "EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    CHUNK_SIZE = getattr(config, "CHUNK_SIZE", 512)
    CHUNK_OVERLAP = getattr(config, "CHUNK_OVERLAP", 64)
    MAX_CHUNK_TEXT_LENGTH = getattr(config, "MAX_CHUNK_TEXT_LENGTH", 5000)
except ImportError:
    MODEL_NAME = "all-MiniLM-L6-v2"
    CHUNK_SIZE = 512
    CHUNK_OVERLAP = 64
    MAX_CHUNK_TEXT_LENGTH = 5000

try:
    from sentence_transformers import SentenceTransformer
except ImportError as e:
    raise RuntimeError("Install with: pip install sentence-transformers") from e


# ─────────────── Embeddings ─────────────────────────────────
@lru_cache(maxsize=1)
def _get_model() -> SentenceTransformer:
    LOGGER.info("Loading embedding model '%s' ...", MODEL_NAME)
    return SentenceTransformer(MODEL_NAME)


def embed_texts(texts: List[str]) -> List[List[float]]:
    if not texts:
        return []
    model = _get_model()
    return model.encode(texts, show_progress_bar=False).tolist()


def embed_text(text: str) -> List[float]:
    return embed_texts([text])[0]


# ─────────────── Chunking ───────────────────────────────────
def split_text(text: str, chunk_size: int = None, overlap: int = None) -> List[str]:
    """Sentence-aware chunking with overlap."""
    if chunk_size is None:
        chunk_size = CHUNK_SIZE
    if overlap is None:
        overlap = CHUNK_OVERLAP
    if not text:
        return []
    sentences = sent_tokenize(text)
    chunks = []
    chunk = ""
    for sentence in sentences:
        if len(chunk) + len(sentence) <= chunk_size:
            chunk += " " + sentence
        else:
            if chunk.strip():
                chunks.append(chunk.strip()[:MAX_CHUNK_TEXT_LENGTH])
            chunk = sentence[-overlap:] if len(sentence) > overlap else sentence
    if chunk.strip():
        chunks.append(chunk.strip()[:MAX_CHUNK_TEXT_LENGTH])
    return chunks


# ─────────────── Extractors ─────────────────────────────────
def _extract_pdf(path: Path) -> str:
    if fitz is None:
        return ""
    try:
        with fitz.open(str(path)) as doc:
            return "\n".join(page.get_text() for page in doc)
    except Exception as e:
        LOGGER.error("PDF extract failed [%s]: %s", path.name, e)
        return ""


def _extract_docx(path: Path) -> str:
    if Document is None:
        return ""
    try:
        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs if p.text]
        # Redline/comparison documents keep most content in tables.
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts)
    except Exception as e:
        LOGGER.error("DOCX extract failed [%s]: %s", path.name, e)
        return ""


def _extract_xlsx(path: Path) -> str:
    if pd is not None:
        try:
            dfs = pd.read_excel(str(path), sheet_name=None, nrows=10000)
            parts = []
            for sheet_name, df in dfs.items():
                parts.append(f"--- {sheet_name} ---\n{df.to_string(index=False)}")
            return "\n\n".join(parts)
        except Exception:
            pass
    if openpyxl is None:
        return ""
    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        rows = [
            str(cell)
            for sheet in wb.worksheets
            for row in sheet.iter_rows(values_only=True)
            for cell in row if cell
        ]
        return "\n".join(rows)
    except Exception as e:
        LOGGER.error("XLSX extract failed [%s]: %s", path.name, e)
        return ""


def _extract_xls(path: Path) -> str:
    if xlrd is None:
        return ""
    try:
        book = xlrd.open_workbook(str(path))
        return "\n".join(
            str(cell.value)
            for sheet in book.sheets()
            for r in range(sheet.nrows)
            for cell in sheet.row(r)
            if cell.value
        )
    except Exception as e:
        LOGGER.error("XLS extract failed [%s]: %s", path.name, e)
        return ""


def _extract_csv(path: Path) -> str:
    if pd is not None:
        try:
            df = pd.read_csv(str(path), nrows=10000)
            return df.to_string(index=False)
        except Exception:
            pass
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            reader = csv.reader(f)
            return "\n".join(" | ".join(row) for row in reader)
    except Exception as e:
        LOGGER.error("CSV extract failed [%s]: %s", path.name, e)
        return ""


def _extract_txt(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception as e:
        LOGGER.error("TXT read failed [%s]: %s", path.name, e)
        return ""


def _extract_json(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return json.dumps(data, indent=2)
    except Exception as e:
        LOGGER.error("JSON load failed [%s]: %s", path.name, e)
        return ""


def _extract_html(path: Path) -> str:
    if BeautifulSoup is None:
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            soup = BeautifulSoup(f, "html.parser")
            return soup.get_text(separator="\n")
    except Exception as e:
        LOGGER.error("HTML extract failed [%s]: %s", path.name, e)
        return ""


def _extract_image(path: Path) -> str:
    if Image is None or pytesseract is None:
        return ""
    try:
        img = Image.open(str(path))
        # Pre-process for better OCR: convert to grayscale, increase contrast
        img = img.convert("L")
        return pytesseract.image_to_string(img)
    except Exception as e:
        LOGGER.error("OCR failed [%s]: %s", path.name, e)
        return ""


_EXTRACTORS = {
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
    ".xlsx": _extract_xlsx,
    ".xls": _extract_xls,
    ".csv": _extract_csv,
    ".txt": _extract_txt,
    ".md": _extract_txt,
    ".json": _extract_json,
    ".html": _extract_html,
    ".htm": _extract_html,
    ".jpg": _extract_image,
    ".jpeg": _extract_image,
    ".png": _extract_image,
    ".tif": _extract_image,
    ".tiff": _extract_image,
    ".bmp": _extract_image,
}


def extract_text(path: str | os.PathLike) -> str:
    """Extract raw text from any supported filetype. Falls back through multiple extractors."""
    p = Path(path)
    if not p.exists():
        LOGGER.warning("File not found: %s", path)
        return ""
    ext = p.suffix.lower()

    # Try unstructured first for PDF/DOCX/HTML (best quality)
    try:
        if ext == ".pdf":
            from unstructured.partition.pdf import partition_pdf
            elements = partition_pdf(filename=str(p))
            return "\n\n".join(el.text for el in elements if el.text)
        if ext == ".docx":
            from unstructured.partition.docx import partition_docx
            elements = partition_docx(filename=str(p))
            return "\n\n".join(el.text for el in elements if el.text)
        if ext in (".html", ".htm"):
            from unstructured.partition.html import partition_html
            elements = partition_html(filename=str(p))
            return "\n\n".join(el.text for el in elements if el.text)
    except Exception:
        pass

    # Fallback to registered extractors
    extractor = _EXTRACTORS.get(ext)
    if extractor:
        result = extractor(p)
        if result:
            return result

    # Last-resort Tika fallback for any type
    try:
        from tika import parser as tika_parser
        parsed = tika_parser.from_file(str(p))
        content = (parsed.get("content") or "").strip()
        if content:
            return content
    except Exception:
        pass

    LOGGER.info("No text could be extracted from '%s'", p.name)
    return ""


# ─────────────── High-level pipeline ───────────────────────
def compute_chunks(
    path: str | os.PathLike,
    chunk_size: int = None,
    overlap: int = None,
) -> Tuple[List[str], List[List[float]]]:
    if chunk_size is None:
        chunk_size = CHUNK_SIZE
    if overlap is None:
        overlap = CHUNK_OVERLAP
    text = extract_text(path)
    if not text.strip():
        return [], []
    chunks = split_text(text, chunk_size, overlap)
    if not chunks:
        return [], []
    vectors = embed_texts(chunks)
    return chunks, vectors


def preview_for_file(file_id: int) -> dict:
    from core.models import File
    f = File.objects.filter(pk=file_id).first()
    from django.core.signing import Signer
    signer = Signer()
    if not f:
        return {}
    return {
        "filename": f.filename,
        "signed_url": f"/api/download/?token=" + signer.sign(f.id),
        "size": f.file_size,
        "mime": f.file_type,
    }
