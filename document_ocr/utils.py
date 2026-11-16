import os
import subprocess
import logging
import shutil
import uuid
import hashlib
import fitz  # PyMuPDF
import pandas as pd
from datetime import datetime
from pypdf import PdfReader, PdfWriter
from django.conf import settings
from django.utils.timezone import now
from django.db import transaction
from django.shortcuts import get_object_or_404
from docx import Document
from core.models import File
from document_ocr.models import OCRFile, OCRRun
import glob

# Configure logging
logger = logging.getLogger(__name__)

# Try importing Adobe PDF Services SDK, otherwise fallback
try:
    from adobe.pdfservices.operation.auth.service_principal_credentials import ServicePrincipalCredentials
    from adobe.pdfservices.operation.pdf_services import PDFServices
    from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType
    from adobe.pdfservices.operation.pdfjobs.jobs.export_pdf_job import ExportPDFJob
    from adobe.pdfservices.operation.pdfjobs.params.export_pdf.export_pdf_params import ExportPDFParams
    from adobe.pdfservices.operation.pdfjobs.params.export_pdf.export_pdf_target_format import ExportPDFTargetFormat
    from adobe.pdfservices.operation.pdfjobs.result.export_pdf_result import ExportPDFResult
    ADOBE_PDF_SERVICES_AVAILABLE = True
except ImportError:
    ADOBE_PDF_SERVICES_AVAILABLE = False
    logger.warning("Adobe PDF Services SDK is not installed. Falling back to Pandoc for DOCX conversion.")


class OCRService:
    """Handles OCR processing for PDF files."""

    def get_file_path(self, file_id):
        """Retrieve file path from `core.models.File`."""
        file_obj = File.objects.filter(id=file_id).first()
        if not file_obj:
            logger.error(f"File ID {file_id} not found.")
            return None
        return file_obj.filepath if os.path.exists(file_obj.filepath) else None

    def is_pdf(self, file_path):
        """Check if the file is a valid PDF."""
        try:
            with open(file_path, "rb") as f:
                PdfReader(f)
            return True
        except:
            return False

    def get_output_path(self, file_id, file_path, ocr_option="Basic-ocr"):
        """Generates structured output path for OCRed files."""
        base_name = os.path.basename(file_path)

        # Ensure the OCR directories exist without redundancy
        ocr_dir = os.path.join(os.path.dirname(file_path), "ocr", ocr_option.lower())  # Separate directories for OCR options
        os.makedirs(ocr_dir, exist_ok=True)  # Ensure the OCR directory exists

        # Final output path for OCR file
        output_path = os.path.join(ocr_dir, f"ocr-{base_name}")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        return output_path

    def get_file_md5(self, file_path):
        """Returns the MD5 hash of the given file to avoid duplicates."""
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def burst_pdf(self, file_path, batch_size=10, ocr_option="Basic-ocr"):
        """Splits a PDF into smaller batches for OCR processing."""
        pdf_reader = PdfReader(file_path)
        total_pages = len(pdf_reader.pages)

        # Ensure tmp_dir is under ocr/{ocr_option}/tmp/
        ocr_dir = os.path.join(os.path.dirname(file_path), "ocr", ocr_option.lower())  # Corrected path for OCR option
        tmp_dir = os.path.join(ocr_dir, "tmp")  # Ensure tmp is under the OCR option directory
        os.makedirs(tmp_dir, exist_ok=True)

        burst_files = []
        for start_page in range(0, total_pages, batch_size):
            end_page = min(start_page + batch_size, total_pages)
            pdf_writer = PdfWriter()

            for page in range(start_page, end_page):
                pdf_writer.add_page(pdf_reader.pages[page])

            burst_filepath = os.path.join(tmp_dir, f"ocr-part-{uuid.uuid4()}.pdf")
            with open(burst_filepath, "wb") as batch_file:
                pdf_writer.write(batch_file)

            burst_files.append((start_page + 1, end_page, burst_filepath))

        return burst_files


    def apply_ocr(self, file_id, file_path, ocr_option="Basic-ocr"):
        """Applies OCR to a given PDF file and saves the OCRed file."""
        if not file_path or not self.is_pdf(file_path):
            logger.info(f"Skipping OCR: File {file_path} is not a PDF.")
            return None

        # Ensure the OCR directories exist without redundancy
        ocr_dir = os.path.join(os.path.dirname(file_path), "ocr", ocr_option.lower())  # Ensure OCR option directory
        tmp_dir = os.path.join(ocr_dir, "tmp")  # Ensure tmp is under the OCR option directory
        os.makedirs(tmp_dir, exist_ok=True)  # Ensure tmp exists

        # Store OCR output in `tmp/`
        ocr_output_path = os.path.join(tmp_dir, f"ocr-{uuid.uuid4()}.pdf")  # Save OCR output directly in tmp/

        try:
            logger.info(f"Applying OCR to {file_path}")

            if ocr_option.lower() == "basic-ocr":
                cmd = [
                    "ocrmypdf",
                    "--optimize", "1",
                    "--force-ocr",
                    "--rotate-pages",
                    file_path,
                    ocr_output_path
                ]
                process = subprocess.run(cmd, capture_output=True, text=True)

            elif ocr_option.lower() == "advanced-ocr":
                # Advanced OCR using Tesseract via ImageMagick and pypdf
                output_image_pattern = file_path.replace('.pdf', '_page_%d.png')
                cmd_convert = [
                    'magick',
                    '-density', '300',  # High DPI for better OCR accuracy
                    file_path,
                    output_image_pattern
                ]
                subprocess.run(cmd_convert, check=True)

                ocr_output_files = []
                for image_file in sorted(glob.glob(output_image_pattern.replace('%d', '*'))):
                    ocr_output_pdf = image_file.replace('.png', '.pdf')
                    cmd_ocr = [
                        'tesseract',
                        image_file,
                        ocr_output_pdf.replace('.pdf', ''),
                        '--oem', '1',
                        '--psm', '3',
                        'pdf'
                    ]
                    subprocess.run(cmd_ocr, check=True)
                    ocr_output_files.append(ocr_output_pdf)

                if ocr_output_files:
                    self.merge_pdf(ocr_output_files, ocr_output_path, ocr_option)

            else:
                raise ValueError("Invalid OCR option provided.")

            logger.info(f"OCR applied successfully using {ocr_option}. Output saved to {ocr_output_path}")
            return ocr_output_path

        except subprocess.CalledProcessError as e:
            logger.error(f"OCR processing failed: {e.stderr}")
            return None



    def merge_pdf(self, ocr_output_files, output_path, ocr_option):
        """Merges all PDFs found under the ocr/{ocr_option} directory into a single file."""
        try:
            # Ensure the OCR directories exist
            ocr_dir = os.path.join(os.path.dirname(output_path), "ocr", ocr_option.lower())  # Define base OCR directory
            os.makedirs(ocr_dir, exist_ok=True)

            # If files were passed explicitly, use them; otherwise, search the OCR directory for PDF files
            if not ocr_output_files:
                # Find all PDF files under the OCR directory (recursively)
                ocr_files = glob.glob(os.path.join(ocr_dir, '**', '*.pdf'), recursive=True)
                if not ocr_files:
                    logger.error(f"No OCR batches found to merge in {ocr_dir}.")
                    return None
                ocr_output_files = ocr_files

            logger.info(f"Found {len(ocr_output_files)} PDFs to merge.")

            # Merge PDFs using PdfMerger
            merger = PdfWriter()
            for pdf in ocr_output_files:
                logger.info(f"Adding {pdf} to the merger.")
                merger.append(pdf)

            # Write the merged PDF to the final output path
            with open(output_path, "wb") as output_file:
                merger.write(output_file)

            logger.info(f"Merged PDF saved to: {output_path}")

            # Move the merged file to the desired location (above the tmp directory)
            final_pdf_dir = os.path.join(ocr_dir, "final")
            os.makedirs(final_pdf_dir, exist_ok=True)
            final_pdf_path = os.path.join(final_pdf_dir, "final_merged_ocr_file.pdf")
            shutil.move(output_path, final_pdf_path)

            logger.info(f"Final merged PDF moved to: {final_pdf_path}")

            # Clean up the temporary files (everything under tmp/)
            tmp_dir = os.path.join(ocr_dir, "tmp")

            try:
                # Clean up the temporary files (everything under tmp/)
                shutil.rmtree(tmp_dir)
                logger.info(f"Removed temp directory: {tmp_dir}")
            except Exception as e:
                # If tmp directory doesn't exist, handle final merged file logic for nested tmp directories
                logger.error(f"Error during temp directory cleanup: {e}")

                # Handle cleanup for Advanced-OCR with nested tmp directories
                # Dynamically find and remove nested tmp directories
                if ocr_option.lower() == "advanced-ocr":
                    # Find nested tmp directories
                    nested_tmp_dirs = glob.glob(os.path.join(ocr_dir, '**', 'tmp'), recursive=True)
                    for nested_tmp in nested_tmp_dirs:
                        if os.path.exists(nested_tmp):
                            shutil.rmtree(nested_tmp)
                            logger.info(f"Removed nested temp directory: {nested_tmp}")
                        else:
                            logger.warning(f"Could not trace directory: {nested_tmp}")

            # Handle cleanup for final merged file and tmp dir logic
            final_dir = os.path.join(ocr_dir, "final")
            final_position_of_merged_file = final_dir.rsplit('/tmp', 1)[0]
            dir_to_delete = os.path.join(final_position_of_merged_file, "tmp")

            if os.path.exists(final_dir):
                final_merged_file = os.path.join(final_dir, "final_merged_ocr_file.pdf")

                if os.path.exists(final_merged_file):
                    logger.info(f"Final merged file found: {final_merged_file}")

                    # Ensure final destination exists
                    if not os.path.exists(final_position_of_merged_file):
                        os.makedirs(final_position_of_merged_file)
                        logger.info(f"Created directory: {final_position_of_merged_file}")

                    # Move the final merged file
                    shutil.move(final_merged_file, final_position_of_merged_file)
                    logger.info(f"Moved final merged file to: {final_position_of_merged_file}")

                    # Remove the tmp directory
                    if os.path.exists(dir_to_delete):
                        shutil.rmtree(dir_to_delete)
                        logger.info(f"Removed temp directory: {dir_to_delete}")
                    else:
                        logger.warning(f"Could not trace directory: {dir_to_delete}")
                else:
                    logger.warning(f"Final merged file not found in: {final_dir}")

                return final_merged_file
            else:
                logger.warning(f"Final directory not found: {final_dir}")
                return final_pdf_path

            return final_pdf_path

        except Exception as e:
            logger.error(f"Error during PDF merging: {str(e)}")
            return None





    '''
    def merge_pdf(self, ocr_output_files, output_path, ocr_option):
        """Merges all PDFs found under the ocr/{ocr_option} directory into a single file."""
        try:
            # Ensure the OCR directories exist
            ocr_dir = os.path.join(os.path.dirname(output_path), "ocr", ocr_option.lower())  # Define base OCR directory
            os.makedirs(ocr_dir, exist_ok=True)

            # If files were passed explicitly, use them; otherwise, search the OCR directory for PDF files
            if not ocr_output_files:
                # Find all PDF files under the OCR directory (recursively)
                ocr_files = glob.glob(os.path.join(ocr_dir, '**', '*.pdf'), recursive=True)
                if not ocr_files:
                    logger.error(f"No OCR batches found to merge in {ocr_dir}.")
                    return None
                ocr_output_files = ocr_files

            logger.info(f"Found {len(ocr_output_files)} PDFs to merge.")

            # Merge PDFs using PdfMerger
            merger = PdfWriter()
            for pdf in ocr_output_files:
                logger.info(f"Adding {pdf} to the merger.")
                merger.append(pdf)

            # Write the merged PDF to the final output path
            with open(output_path, "wb") as output_file:
                merger.write(output_file)

            logger.info(f"Merged PDF saved to: {output_path}")

            # Move the merged file to the desired location (above the tmp directory)
            final_pdf_dir = os.path.join(ocr_dir, "final")
            os.makedirs(final_pdf_dir, exist_ok=True)
            final_pdf_path = os.path.join(final_pdf_dir, "final_merged_ocr_file.pdf")
            shutil.move(output_path, final_pdf_path)

            logger.info(f"Final merged PDF moved to: {final_pdf_path}")

            # Clean up the temporary files (everything under tmp/)
            

            try:
                # Clean up the temporary files (everything under tmp/)
                tmp_dir = os.path.join(ocr_dir, "tmp")
                shutil.rmtree(tmp_dir)
                logger.info(f"Removed temp directory: {tmp_dir}")
            except Exception as e:
                # If tmp directory doesn't exist, handle final merged file logic
                logger.error(f"Error during temp directory cleanup: {e}")
                
                # Define the final directory location
                final_dir = os.path.join(ocr_dir, "final")
                final_position_of_merged_file = final_dir.rsplit('/tmp', 1)[0]
                dir_to_delete = os.path.join(final_position_of_merged_file , "tmp")
                
                # Check if final directory exists
                if os.path.exists(final_dir):
                    final_merged_file = os.path.join(final_dir, "final_merged_ocr_file.pdf")
                    
                    # Check if final merged file exists
                    if os.path.exists(final_merged_file):
                        logger.info(f"Final merged file found: {final_merged_file}")
                        
                        # Ensure final destination exists
                        if not os.path.exists(final_position_of_merged_file):
                            os.makedirs(final_position_of_merged_file)
                            logger.info(f"Created directory: {final_position_of_merged_file}")
                        
                        # Move the final merged file
                        shutil.move(final_merged_file, final_position_of_merged_file)
                        logger.info(f"Moved final merged file to: {final_position_of_merged_file}")
                        
                        # Remove the tmp directory
                        if os.path.exists(dir_to_delete):
                            shutil.rmtree(dir_to_delete)
                            logger.info(f"Removed temp directory: {dir_to_delete}")
                        else:
                            logger.warning(f"Could not trace directory: {dir_to_delete}") 
                    else:
                        logger.warning(f"Final merged file not found in: {final_dir}")

                    return final_merged_file

                else:
                    logger.warning(f"Final directory not found: {final_dir}")
                    return final_pdf_path

            # Clean up the temporary files (everything under tmp/)
            # tmp_dir = os.path.join(ocr_dir, "tmp")
            # if os.path.exists(tmp_dir):
            #     shutil.rmtree(tmp_dir)
            #     logger.info(f"Removed temp directory: {tmp_dir}")
            # else:
            #     logger.warning(f"Temp directory not found: {tmp_dir}")
            #

            return final_pdf_path

        except Exception as e:
            logger.error(f"Error during PDF merging: {str(e)}")
            return None
        '''


    def convert_to_formatted_docx(self, pdf_path, output_path):
        """Converts OCRed PDF to **Formatted DOCX** using Adobe PDF Services."""
        if not ADOBE_PDF_SERVICES_AVAILABLE:
            logger.warning("Adobe PDF Services not available. Skipping formatted DOCX conversion.")
            return None

        try:
            credentials = ServicePrincipalCredentials(
                client_id=os.getenv("PDF_SERVICES_CLIENT_ID"),
                client_secret=os.getenv("PDF_SERVICES_CLIENT_SECRET")
            )
            pdf_services = PDFServices(credentials=credentials)

            with open(pdf_path, "rb") as file:
                input_stream = file.read()

            input_asset = pdf_services.upload(input_stream=input_stream, mime_type=PDFServicesMediaType.PDF)
            export_pdf_params = ExportPDFParams(target_format=ExportPDFTargetFormat.DOCX)
            export_pdf_job = ExportPDFJob(input_asset=input_asset, export_pdf_params=export_pdf_params)

            location = pdf_services.submit(export_pdf_job)
            pdf_services_response = pdf_services.get_job_result(location, ExportPDFResult)
            result_asset = pdf_services_response.get_result().get_asset()
            stream_asset = pdf_services.get_content(result_asset)

            with open(output_path, "wb") as docx_file:
                docx_file.write(stream_asset.get_input_stream())

            return output_path

        except Exception as e:
            logger.error(f"Formatted DOCX conversion failed: {e}")
            return None

    def convert_to_raw_docx(self, pdf_path, output_path):
        """Converts OCRed PDF to **Raw DOCX** using Pandoc."""
        try:
            cmd = ["pandoc", pdf_path, "-o", output_path]
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return output_path
        except subprocess.CalledProcessError as e:
            logger.error(f"Raw DOCX conversion failed: {e.stderr.decode()}")
            return None

    def extract_bookmarks_to_dataframe(self, input_pdf):
        """
        Extract bookmarks from the input PDF and store them in a pandas DataFrame.
        """
        bookmarks = []
        pdf_document = fitz.open(input_pdf)
        outlines = pdf_document.get_toc()

        for outline in outlines:
            level, title, page_num = outline
            bookmarks.append({"bookmark": title, "line": level, "page": page_num - 1})

        df_bookmarks = pd.DataFrame(bookmarks)
        pdf_document.close()
        return df_bookmarks


    def reattach_bookmarks_from_dataframe(self, final_ocr_pdf, df_bookmarks, start_page, end_page):
        """
        Reattaches bookmarks to the extracted pages using the pandas DataFrame.
        """
        pdf_document = fitz.open(final_ocr_pdf)
        toc = []  # This will hold the new Table of Contents (TOC) items

        # Iterate over the bookmarks and add them back if they are within the page range
        for _, row in df_bookmarks.iterrows():
            if start_page <= row['page'] < end_page:
                # Adjust the page number for the new document
                adjusted_page_num = row['page'] - (start_page) + 1
                # Append the bookmark to the new TOC list
                toc.append([row['line'], row['bookmark'], adjusted_page_num])
                logger.info(f"Reattached Bookmark: '{row['bookmark']}' on New Page: {adjusted_page_num}")

        # Set the TOC back to the new document
        pdf_document.set_toc(toc)

        # Save the updated PDF without incremental saving (overwrite the original file)
        final_ocr_pdf_with_bookmarks = final_ocr_pdf.replace(".pdf", "_with_bookmarks.pdf")
        pdf_document.save(final_ocr_pdf_with_bookmarks, incremental=False)
        pdf_document.close()
        logger.info(f"Final PDF with bookmarks saved to {final_ocr_pdf_with_bookmarks}")

        return final_ocr_pdf_with_bookmarks


    def cleanup_tmp_dir(self, ocr_dir):
        """Removes the temporary directories used during OCR processing."""
        # The OCR options you are expecting
        options = ['basic-ocr', 'advanced-ocr']
        
        # Check which OCR option is in the path
        for option in options:
            if option in ocr_dir:
                # Split the path at the OCR option (either 'basic-ocr' or 'advanced-ocr') and reconstruct the tmp path
                tmp_dir = ocr_dir.split(option)[0] + option + "/tmp"
                
                # Check if tmp_dir exists and remove it
                if os.path.exists(tmp_dir):
                    shutil.rmtree(tmp_dir)  # Remove the tmp directory
                    logger.info(f"Removed temp directory: {tmp_dir}")
                else:
                    logger.warning(f"Temp directory not found: {tmp_dir}")
                return
        
        # If no valid OCR option is found in the path
        logger.error(f"Invalid OCR option in path: {ocr_dir}")




def cleanup_tmp_dir(tmp_dir):
    """Removes temporary directories."""
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.info(f"Cleaned up temporary directory: {tmp_dir}")

def cleanup_temp_files(file_paths):
    """Delete temporary files."""
    for path in file_paths:
        os.remove(path)
        logger.info(f"Deleted temporary file: {path}")

