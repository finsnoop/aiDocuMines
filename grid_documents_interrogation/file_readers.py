# file_readers.py

import pandas as pd
from docx import Document
import fitz
import os
import shutil
import subprocess
import glob

import pytesseract
from PIL import Image
import tempfile

import hashlib



def read_csv_file(file_path):
    try:
        return pd.read_csv(file_path)
    except Exception as e:
        print(f"Error reading CSV: {e}")
        return None

def read_txt_file(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except UnicodeDecodeError:
        with open(file_path, 'r', encoding='latin1') as f:
            return f.read()

def read_excel_file(file_path):
    ext = file_path.lower().split(".")[-1]
    try:
        if ext == "xlsx":
            from openpyxl import load_workbook
            wb = load_workbook(filename=file_path, data_only=True)
            sheet = wb.active
            data = sheet.values
            columns = next(data)
            return pd.DataFrame(data, columns=columns)
        elif ext == "xls":
            import xlrd
            wb = xlrd.open_workbook(file_path)
            sheet = wb.sheet_by_index(0)
            data = [sheet.row_values(i) for i in range(sheet.nrows)]
            return pd.DataFrame(data[1:], columns=data[0])
        else:
            raise ValueError("Unsupported Excel file format")
    except Exception as e:
        print(f"Error reading Excel: {e}")
        return None

def read_docx_file(file_path):
    try:
        doc = Document(file_path)
        return "\n".join(para.text for para in doc.paragraphs)
    except Exception as e:
        print(f"Error reading DOCX: {e}")
        return None



def read_pdf_file(file_path):
    """Actual questions may recover a scanned PDF; short native text is valid."""
    from document_search.utils import extract_text
    return extract_text(file_path, allow_ocr=True)


def read_file(file_path):
    ext = file_path.rsplit('.', 1)[-1].lower() if '.' in file_path else ''

    # ── Tabular ──
    if ext == 'csv':
        return read_csv_file(file_path), True
    if ext in ('xlsx', 'xls'):
        return read_excel_file(file_path), True

    # ── Documents ──
    if ext == 'txt':
        return read_txt_file(file_path), False
    if ext == 'docx':
        return read_docx_file(file_path), False
    if ext == 'pdf':
        return read_pdf_file(file_path), False
    if ext == 'pptx':
        return read_pptx_file(file_path), False

    # ── Web / markup ──
    if ext in ('html', 'htm'):
        return read_html_file(file_path), False
    if ext == 'md':
        return read_md_file(file_path), False

    # ── Structured data / config ──
    if ext in ('json', 'xml', 'svg', 'log', 'ini', 'cfg', 'yaml', 'yml',
               'toml', 'env', 'conf', 'properties', 'sql', 'graphql'):
        return read_md_file(file_path), False

    # ── Source code (any language) ──
    if ext in ('py', 'js', 'ts', 'tsx', 'jsx', 'java', 'c', 'cpp', 'h', 'hpp',
               'cs', 'go', 'rs', 'rb', 'php', 'swift', 'kt', 'scala', 'r',
               'm', 'mm', 'dart', 'lua', 'pl', 'sh', 'bash', 'zsh', 'fish',
               'ps1', 'bat', 'cmd', 'ps1', 'vbs', 'makefile', 'cmake',
               'dockerfile', 'tf', 'hcl'):
        return read_md_file(file_path), False

    # ── Images ──
    if ext in ('png', 'jpg', 'jpeg', 'tiff', 'tif', 'bmp', 'gif', 'webp'):
        return read_image_file(file_path), False

    # ── Email ──
    if ext in ('eml', 'msg'):
        return read_md_file(file_path), False

    # ── Office legacy / other text-ish ──
    if ext in ('rtf', 'odt', 'odp', 'ods'):
        result = _read_libreoffice_file(file_path)
        if result:
            return result, False

    # ── OpenXML (without python-pptx) — docx/xlsx/pptx fallback ──
    if ext in ('docx', 'xlsx', 'pptx'):
        import zipfile, xml.etree.ElementTree as ET
        try:
            with zipfile.ZipFile(file_path, 'r') as z:
                parts = [n for n in z.namelist() if n.endswith('.xml')]
                text_parts = []
                for part in parts:
                    try:
                        tree = ET.parse(z.open(part))
                        for elem in tree.iter():
                            if elem.text and elem.text.strip():
                                text_parts.append(elem.text.strip())
                    except Exception:
                        continue
                if text_parts:
                    return ' '.join(text_parts), False
        except Exception:
            pass

    # ── Universal fallback: try reading as text ──
    result = _read_as_text_fallback(file_path)
    if result:
        return result, False

    # ── Last resort: binary sniff ──
    result = _read_binary_fallback(file_path)
    if result:
        return result, False

    print(f"Unsupported file type: {file_path}")
    return None, False


def _read_as_text_fallback(file_path):
    """Try reading any file as UTF-8 / Latin-1 text."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            text = f.read()
        if text.strip():
            return text
    except (UnicodeDecodeError, UnicodeError):
        try:
            with open(file_path, 'r', encoding='latin-1') as f:
                text = f.read()
            if text.strip():
                return text
        except Exception:
            pass
    except Exception:
        pass
    return None


def _read_binary_fallback(file_path):
    """For truly binary files: extract any readable ASCII/UTF-8 runs."""
    try:
        import subprocess
        # 'strings' extracts printable text from binary files
        result = subprocess.run(
            ['strings', '-n', '4', file_path],
            capture_output=True, text=True, timeout=30
        )
        text = result.stdout.strip()
        if len(text) > 100:
            return text
    except Exception:
        pass
    return None


def _read_libreoffice_file(file_path):
    """Convert .odt/.ods/.odp/.rtf to text via LibreOffice (if installed)."""
    try:
        import subprocess, tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(
                ['libreoffice', '--headless', '--convert-to', 'txt:Text',
                 '--outdir', tmpdir, file_path],
                capture_output=True, timeout=60
            )
            import glob
            txt_files = glob.glob(os.path.join(tmpdir, '*.txt'))
            if txt_files:
                with open(txt_files[0], 'r', encoding='utf-8', errors='replace') as f:
                    return f.read()
    except Exception:
        pass
    return None

def convert_dataframe_to_text(df):
    return df.to_string() if isinstance(df, pd.DataFrame) else str(df)


def read_pptx_file(file_path):
    try:
        from pptx import Presentation
        prs = Presentation(file_path)
        texts = []
        for slide_num, slide in enumerate(prs.slides, 1):
            slide_texts = []
            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text.strip():
                    slide_texts.append(shape.text.strip())
            if slide_texts:
                texts.append(f"[Slide {slide_num}]\n" + "\n".join(slide_texts))
        return "\n\n".join(texts) if texts else None
    except Exception as e:
        print(f"Error reading PPTX: {e}")
        return None

def read_html_file(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except Exception as e:
        print(f"Error reading HTML: {e}")
        return None

def read_md_file(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except Exception as e:
        print(f"Error reading Markdown: {e}")
        return None

def read_image_file(file_path):
    try:
        temp_txt_path = file_path + ".ocr.txt"

        # Run OCR directly on the image (PNG, JPG, etc.)
        subprocess.run([
            'tesseract',
            file_path,
            temp_txt_path.replace(".txt", ""),  # Tesseract appends `.txt`
            '--oem', '1',
            '--psm', '3'
        ], check=True)

        with open(temp_txt_path, 'r') as f:
            text = f.read()

        os.remove(temp_txt_path)
        return text
    except Exception as e:
        print(f"Error reading image OCR: {e}")
        return None

