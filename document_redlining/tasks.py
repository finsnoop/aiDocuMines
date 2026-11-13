import os
import logging
import uuid
from celery import shared_task
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.timezone import now
from core.models import File
from core.utils import register_generated_file
from document_redlining.models import RedliningRun, RedliningResult
from document_redlining.utils import compare_text_files, compare_docx_files, extract_text_from_file, generate_pdf_with_change_bars


logger = logging.getLogger(__name__)


@shared_task
def perform_redlining_task(file_id, comparison_file_id, run_id, author="Redlining System"):
    logger.info(f"Starting redlining for file_id={file_id}, comparison_file_id={comparison_file_id}, run_id={run_id}")

    file_entry = get_object_or_404(File, id=file_id)
    comparison_entry = get_object_or_404(File, id=comparison_file_id)
    redlining_run = get_object_or_404(RedliningRun, id=run_id)

    if not os.path.exists(file_entry.filepath):
        logger.error(f"Original file not found: {file_entry.filepath}")
        redlining_run.status = "Failed"
        redlining_run.error_message = "Original file not found"
        redlining_run.save()
        return {"error": "Original file not found", "file_id": file_id}

    if not os.path.exists(comparison_entry.filepath):
        logger.error(f"Comparison file not found: {comparison_entry.filepath}")
        redlining_run.status = "Failed"
        redlining_run.error_message = "Comparison file not found"
        redlining_run.save()
        return {"error": "Comparison file not found", "file_id": file_id}

    try:
        ext1 = os.path.splitext(file_entry.filepath)[-1].lower()
        ext2 = os.path.splitext(comparison_entry.filepath)[-1].lower()
        supported_exts = {".txt", ".docx", ".pdf", ".html", ".htm"}

        if ext1 not in supported_exts or ext2 not in supported_exts:
            raise ValueError(f"Unsupported file type(s): {ext1}, {ext2}")

        diff_dir = os.path.join(os.path.dirname(file_entry.filepath), "redlining", str(uuid.uuid4()))
        os.makedirs(diff_dir, exist_ok=True)

        if ext1 == ".docx" or ext2 == ".docx":
            result = compare_docx_files(
                file_entry.filepath,
                comparison_entry.filepath,
                author=author,
            )
            redline_docx_path = result.get("redline_docx_path")
            diff_html = result.get("diff_html", "")
            stats = result.get("stats", {})

            output_pdf_path = os.path.join(diff_dir, f"redline_{os.path.splitext(file_entry.filename)[0]}_vs_{comparison_entry.filename}.pdf")
            redline_pdf_path = generate_pdf_with_change_bars(redline_docx_path, output_pdf_path)
        else:
            result = compare_text_files(file_entry.filepath, comparison_entry.filepath)
            redline_docx_path = None
            redline_pdf_path = None
            diff_html = result.get("diff_html", "")
            stats = {
                "added_lines": result.get("added_lines", 0),
                "removed_lines": result.get("removed_lines", 0),
                "modified_lines": 0,
                "unchanged_lines": result.get("unchanged_lines", 0),
            }

        diff_filename = f"redlining_{os.path.splitext(file_entry.filename)[0]}_vs_{comparison_entry.filename}.html"
        diff_output_path = os.path.join(diff_dir, diff_filename)

        with open(diff_output_path, "w", encoding="utf-8") as f:
            f.write(diff_html)

        with transaction.atomic():
            redlining_result, created = RedliningResult.objects.update_or_create(
                original_file=file_entry,
                comparison_file=comparison_entry,
                run=redlining_run,
                defaults={
                    "diff_output_path": diff_output_path,
                    "diff_html": diff_html,
                    "redline_docx_path": redline_docx_path,
                    "redline_pdf_path": redline_pdf_path,
                    "comparison_stats": stats,
                    "author": author,
                    "status": "Completed",
                    "updated_at": now(),
                }
            )

            redlining_run.status = "Completed"
            redlining_run.save()

        registered_outputs = []

        if os.path.exists(diff_output_path):
            registered_html = register_generated_file(
                file_path=diff_output_path,
                user=file_entry.user,
                run=file_entry.run,
                project_id=file_entry.project_id,
                service_id=file_entry.service_id,
                folder_name=os.path.join("redlining", str(redlining_run.id))
            )
            registered_outputs.append({
                "filename": registered_html.filename,
                "file_id": registered_html.id,
                "path": registered_html.filepath,
                "type": "html",
            })

        if redline_docx_path and os.path.exists(redline_docx_path):
            registered_docx = register_generated_file(
                file_path=redline_docx_path,
                user=file_entry.user,
                run=file_entry.run,
                project_id=file_entry.project_id,
                service_id=file_entry.service_id,
                folder_name=os.path.join("redlining", str(redlining_run.id))
            )
            registered_outputs.append({
                "filename": registered_docx.filename,
                "file_id": registered_docx.id,
                "path": registered_docx.filepath,
                "type": "docx",
            })

        if redline_pdf_path and os.path.exists(redline_pdf_path):
            registered_pdf = register_generated_file(
                file_path=redline_pdf_path,
                user=file_entry.user,
                run=file_entry.run,
                project_id=file_entry.project_id,
                service_id=file_entry.service_id,
                folder_name=os.path.join("redlining", str(redlining_run.id))
            )
            registered_outputs.append({
                "filename": registered_pdf.filename,
                "file_id": registered_pdf.id,
                "path": registered_pdf.filepath,
                "type": "pdf",
            })

        logger.info(f"Redlining completed for run_id={run_id}, file_id={file_id}")
        return {
            "run_id": str(redlining_run.id),
            "file_id": file_id,
            "comparison_file_id": comparison_file_id,
            "status": "Completed",
            "diff_output_path": diff_output_path,
            "redline_docx_path": redline_docx_path,
            "redline_pdf_path": redline_pdf_path,
            "stats": stats,
            "registered_outputs": registered_outputs,
        }

    except Exception as e:
        logger.error(f"Redlining failed for run_id={run_id}: {e}")
        redlining_run.status = "Failed"
        redlining_run.error_message = str(e)
        redlining_run.save()
        return {"error": str(e), "file_id": file_id}
