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



# Top of file_readers.py
OCR_CACHE_DIR = "media/ocr_cache"

os.makedirs(OCR_CACHE_DIR, exist_ok=True)  # Make sure it exists


# Allow large images (disable Pillow safety)
Image.MAX_IMAGE_PIXELS = None  # Only do this for trusted PDFs

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



def compute_ocr_cache_path(file_path):
    """
    Generate a unique OCR cache path using a hash of the absolute file path.
    Ensures no mixing between clients or topics.
    """
    ocr_cache_dir = os.path.join(os.path.dirname(file_path), ".ocr_cache")
    os.makedirs(ocr_cache_dir, exist_ok=True)

    file_hash = hashlib.sha256(file_path.encode()).hexdigest()[:16]
    filename = f"{file_hash}.ocr.txt"
    return os.path.join(ocr_cache_dir, filename)



def perform_ocr_on_pdf(file_path):
    """
    OCR with caching: Avoid repeated OCR if .txt exists AND is non-empty.
    """
    ocr_cache_path = file_path + ".ocr.txt"

    if os.path.exists(ocr_cache_path):
        with open(ocr_cache_path, "r", encoding="utf-8") as f:
            cached_text = f.read().strip()
            if cached_text:
                print(f"[OCR] Using cached OCR from {ocr_cache_path}")
                return cached_text
            else:
                print(f"[OCR] Cached OCR is empty, re-running OCR for {file_path}")

    print(f"[OCR] Starting lightweight OCR on {file_path}")
    try:
        doc = fitz.open(file_path)
        extracted_text = []

        for page_number in range(len(doc)):
            print(f"[OCR] Rendering page {page_number + 1}/{len(doc)}")
            try:
                pix = doc.load_page(page_number).get_pixmap(dpi=96)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as img_temp:
                    pix.save(img_temp.name)
                    image = Image.open(img_temp.name)

                    # safety check
                    if image.size[0] * image.size[1] > 178_956_970:
                        print(f"[⚠️ Skipping page {page_number + 1}] Too large, possible decompression bomb")
                        continue
                    if image.size[0] > 2000 or image.size[1] > 2000:
                        print(f"[⚠️ Resizing large image {image.size}]")
                        image = image.resize((int(image.width / 2), int(image.height / 2)))

                    text = pytesseract.image_to_string(image)
                    extracted_text.append(text)
                    os.remove(img_temp.name)
            except Exception as page_err:
                print(f"[⚠️ Skipping page {page_number + 1}] Reason: {page_err}")
                continue

        full_text = "\n".join(extracted_text).strip()
        print(f"[OCR] OCR complete. Extracted {len(full_text)} characters.")

        if full_text:
            with open(ocr_cache_path, "w", encoding="utf-8") as f:
                f.write(full_text)

        return full_text

    except Exception as e:
        print(f"[❌ OCR ERROR] Failed completely: {e}")
        return ""




'''
def perform_ocr_on_pdf(file_path):
    """
    Perform lightweight OCR on scanned PDF, with cache per file (by path hash).
    """
    cache_path = compute_ocr_cache_path(file_path)

    # Use cache if it exists and is not empty
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            cached_text = f.read()
            if cached_text.strip():
                print(f"[OCR] Using cached OCR from {cache_path}")
                return cached_text
            else:
                print(f"[⚠️ OCR] Cache found but is empty. Re-attempting OCR...")

    print(f"[OCR] Starting OCR on {file_path}")
    try:
        doc = fitz.open(file_path)
        extracted_text = []

        for i, page in enumerate(doc):
            print(f"[OCR] Rendering page {i + 1}/{len(doc)}")
            try:
                pix = page.get_pixmap(dpi=96)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as img_temp:
                    pix.save(img_temp.name)
                    image = Image.open(img_temp.name)

                    # Safety checks
                    if image.size[0] * image.size[1] > 178_956_970:
                        print(f"[⚠️ Skipping page {i+1}] Too large, possible decompression bomb")
                        continue
                    if image.size[0] > 2000 or image.size[1] > 2000:
                        print(f"[⚠️ Resizing large image {image.size}]")
                        image = image.resize((int(image.width / 2), int(image.height / 2)))

                    text = pytesseract.image_to_string(image)
                    extracted_text.append(text)
                    os.remove(img_temp.name)

            except Exception as page_err:
                print(f"[⚠️ Skipping page {i + 1}] Reason: {page_err}")
                continue

        full_text = "\n".join(extracted_text)
        print(f"[OCR] OCR complete. Extracted {len(full_text)} characters.")

        # Cache the result
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(full_text)

        return full_text

    except Exception as e:
        print(f"[❌ OCR ERROR] Failed completely: {e}")
        return ""
'''



'''
def perform_ocr_on_pdf(file_path):
    """
    Safer OCR using PyMuPDF + Tesseract with throttled DPI and decompression bomb handling.
    """
    print(f"[OCR] Starting lightweight OCR on {file_path}")
    try:
        doc = fitz.open(file_path)
        extracted_text = []

        for page_number in range(len(doc)):
            print(f"[OCR] Rendering page {page_number + 1}/{len(doc)}")
            tmp_img_path = None

            try:
                # Use low DPI to reduce image size
                pix = doc.load_page(page_number).get_pixmap(dpi=96)

                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as img_temp:
                    pix.save(img_temp.name)
                    tmp_img_path = img_temp.name

                image = Image.open(tmp_img_path)

                # Optional: resize if too large
                max_size = (2000, 2000)
                if image.width > max_size[0] or image.height > max_size[1]:
                    print(f"[⚠️ Resizing large image {image.size}]")
                    image.thumbnail(max_size)

                text = pytesseract.image_to_string(image)
                extracted_text.append(text)

            except Exception as page_err:
                print(f"[⚠️ Skipping page {page_number + 1}] Reason: {page_err}")
            finally:
                if tmp_img_path and os.path.exists(tmp_img_path):
                    os.remove(tmp_img_path)

        full_text = "\n".join(extracted_text)
        print(f"[OCR] OCR complete. Extracted {len(full_text)} characters.")
        return full_text

    except Exception as e:
        print(f"[❌ OCR ERROR] Failed completely: {e}")
        return ""
'''


'''
def perform_ocr_on_pdf(file_path):
    try:
        output_img_pattern = file_path.replace('.pdf', '_page_%d.png')

        # Step 1: Convert PDF pages to images
        subprocess.run(['magick', '-density', '300', file_path, output_img_pattern], check=True)

        # Step 2: OCR each image to individual PDFs
        ocr_pdfs = []
        for img in sorted(glob.glob(output_img_pattern.replace('%d', '*'))):
            ocr_pdf = img.replace('.png', '.pdf')
            subprocess.run([
                'tesseract',
                img,
                ocr_pdf.replace('.pdf', ''),
                '--oem', '1',
                '--psm', '3',
                'pdf'
            ], check=True)
            ocr_pdfs.append(ocr_pdf)

        # Step 3: Merge OCR PDFs
        merged_pdf = file_path.replace('.pdf', '_OCRed.pdf')
        with open(merged_pdf, 'wb') as out:
            for f in ocr_pdfs:
                with open(f, 'rb') as inp:
                    out.write(inp.read())

        # Step 4: Cleanup temp images and intermediate PDFs
        for f in glob.glob(output_img_pattern.replace('%d', '*')) + ocr_pdfs:
            os.remove(f)

        shutil.move(merged_pdf, file_path)
        return file_path

    except Exception as e:
        print(f"Advanced OCR failed: {e}")
        return file_path
'''


def _pdf_text_layer(file_path):
    """Text already embedded in the PDF (digital documents): fast, exact, no OCR needed."""
    try:
        doc = fitz.open(file_path)
        try:
            text = "\n".join(page.get_text() for page in doc)
        finally:
            doc.close()
        if isinstance(text, str) and text.strip():
            return text
    except Exception:
        pass
    try:
        from pdfminer.high_level import extract_text
        text = extract_text(file_path) or ""
        if text.strip():
            return text
    except Exception:
        pass
    return ""


def read_pdf_file(file_path):
    """
    Returns text content of PDF file: the embedded text layer when there is one,
    OCR only for scanned PDFs (little or no embedded text).
    """
    text = _pdf_text_layer(file_path)
    if len(text.strip()) >= 20:
        return text
    try:
        return perform_ocr_on_pdf(file_path) or text
    except Exception as e:
        print(f"[PDF READ ERROR] {e}")
        return text

'''
def read_pdf_file(file_path):
    try:
        file_path = perform_ocr_on_pdf(file_path)
        doc = fitz.open(file_path)
        return "\n".join(page.get_text() for page in doc)
    except Exception as e:
        print(f"Error reading PDF: {e}")
        return None
'''


def read_file(file_path):
    if file_path.endswith('.csv'):
        return read_csv_file(file_path), True
    elif file_path.endswith('.txt'):
        return read_txt_file(file_path), False
    elif file_path.endswith('.xlsx') or file_path.endswith('.xls'):
        return read_excel_file(file_path), True
    elif file_path.endswith('.docx'):
        return read_docx_file(file_path), False
    elif file_path.endswith('.pdf'):
        return read_pdf_file(file_path), False
    elif file_path.lower().endswith(('.png', '.jpg', '.jpeg', '.tiff')):
        return read_image_file(file_path), False
    else:
        print(f"Unsupported file type: {file_path}")
        return None, False

def convert_dataframe_to_text(df):
    return df.to_string() if isinstance(df, pd.DataFrame) else str(df)


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

