import logging
from typing import List, Optional

from celery import shared_task, chord
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from core.models import BulkJob, BulkJobFileResult, File
from core.bulk_registry import get_bulk_handler
from core.bulk_outputs import (
    build_deliverables, collect_output_files, describe_outputs, summarise_value,
)

logger = logging.getLogger(__name__)

BATCH_CHUNK_SIZE = 50


def get_service_display_name(job_type: str) -> str:
    """Look up display_name from the service catalog."""
    from core.service_catalog import ALL_SERVICES
    # Jobs store the handler name (e.g. "ai_translation"); the catalogue keys
    # services by service_type. Accept either.
    for svc in ALL_SERVICES:
        if job_type in (svc.get("service_type"), svc.get("handler_job_type")):
            return svc.get("display_name", job_type)
    return job_type.replace("_", " ").title()


def get_bulk_output_folder(job: BulkJob):
    """Get or create .batch-output/<service_display_name>/ under the input folder.

    Returns the output Folder or None if job has no folder_id.
    """
    from document_operations.models import Folder

    if not job.folder_id:
        logger.warning(f"BulkJob {job.id} has no folder_id — cannot create output folder")
        return None

    svc_display = get_service_display_name(job.job_type)

    try:
        input_folder = Folder.objects.get(id=job.folder_id)
    except Folder.DoesNotExist:
        logger.warning(f"Input folder {job.folder_id} not found for job {job.id}")
        return None

    batch_root, _ = Folder.objects.get_or_create(
        name=".batch-output",
        parent=input_folder,
        user=job.user,
        project_id=job.project_id,
        service_id=job.service_id,
        defaults={"is_trashed": False},
    )

    output_folder, _ = Folder.objects.get_or_create(
        name=svc_display,
        parent=batch_root,
        user=job.user,
        project_id=job.project_id,
        service_id=job.service_id,
        defaults={"is_trashed": False},
    )

    return output_folder


def _link_into_output_folder(job: BulkJob, files) -> None:
    """Make the job's output folder contain the real output documents."""
    if not files or not job.folder_id:
        return
    from document_operations.models import FileFolderLink

    folder = get_bulk_output_folder(job)
    if folder is None:
        return
    for f in files:
        FileFolderLink.objects.get_or_create(file=f, folder=folder)


def _service_failure(result) -> Optional[str]:
    """Error text if an inline Celery result (or the service's return value) reports failure."""
    if result is None:
        return None
    if hasattr(result, "failed") and result.failed():
        exc = getattr(result, "result", None)
        return f"{type(exc).__name__}: {exc}" if exc else "The service failed"
    value = getattr(result, "result", result)
    if isinstance(value, dict):
        if value.get("error"):
            return str(value["error"])
        if str(value.get("status", "")).lower() in ("failed", "error"):
            return str(value.get("message") or value.get("detail") or "The service reported a failure")
    return None


@shared_task(bind=True)
def process_batch_all_files_in_bulk(self, file_ids: List[int], user_id: int,
                                     job_id: str, job_type: str,
                                     project_id: str = None,
                                     service_id: str = None, **kwargs):
    """Handle a batch_all job: the service processes all files in one call."""
    handler = get_bulk_handler(job_type)
    if not handler:
        logger.error(f"No handler for job_type={job_type}")
        return {"error": f"No handler for {job_type}"}

    started = timezone.now()
    BulkJobFileResult.objects.filter(bulk_job_id=job_id).update(status="Processing", started_at=started)
    logger.info(f"process_batch_all_files_in_bulk: job={job_id}, handler={job_type}, files={len(file_ids)}")
    try:
        result = handler.call_batch(file_ids, user_id, inline=True, project_id=project_id,
                                    service_id=service_id, **kwargs)
        failure = _service_failure(result)
        if failure:
            raise RuntimeError(failure)
        value = getattr(result, "result", result)
        job_fields = summarise_value(value)

        job = BulkJob.objects.get(id=job_id)
        for fid in file_ids:
            input_file = File.objects.filter(id=fid).first()
            outputs = collect_output_files(input_file, started) if input_file else []
            _link_into_output_folder(job, outputs)
            BulkJobFileResult.objects.filter(bulk_job_id=job_id, file_id=fid).update(
                status="Completed",
                result_data={"outputs": describe_outputs(outputs), "fields": job_fields},
                completed_at=timezone.now(),
            )
        BulkJob.objects.filter(id=job_id).update(processed_files=len(file_ids))
        return {"status": "completed", "file_count": len(file_ids)}
    except Exception as exc:
        logger.exception(f"Batch-all handler failed for job {job_id}")
        BulkJobFileResult.objects.filter(bulk_job_id=job_id).update(
            status="Failed", error_message=str(exc)[:4000], completed_at=timezone.now(),
        )
        BulkJob.objects.filter(id=job_id).update(failed_files=len(file_ids))
        return {"error": str(exc), "file_count": len(file_ids)}


@shared_task(bind=True)
def process_single_file_in_bulk(self, file_id: int, user_id: int, job_id: str,
                                 job_type: str, project_id: str = None,
                                 service_id: str = None, **kwargs):
    """
    Run the service for one document and wait for it, so the per-file status is
    true, then record the real output documents and the service's key results.
    """
    handler = get_bulk_handler(job_type)
    if not handler:
        logger.error(f"No handler for job_type={job_type}")
        return {"error": f"No handler for {job_type}", "file_id": file_id}

    started = timezone.now()
    BulkJobFileResult.objects.filter(bulk_job_id=job_id, file_id=file_id).update(
        status="Processing", started_at=started, task_id=self.request.id,
    )
    try:
        result = handler.call(file_id, user_id, inline=True, project_id=project_id,
                              service_id=service_id, **kwargs)
        failure = _service_failure(result)
        if failure:
            raise RuntimeError(failure)
        value = getattr(result, "result", result)

        input_file = File.objects.get(id=file_id)
        outputs = collect_output_files(input_file, started)
        job = BulkJob.objects.get(id=job_id)
        _link_into_output_folder(job, outputs)

        result_data = {"outputs": describe_outputs(outputs), "fields": summarise_value(value)}
        with transaction.atomic():
            BulkJobFileResult.objects.filter(bulk_job_id=job_id, file_id=file_id).update(
                status="Completed", result_data=result_data, completed_at=timezone.now(),
            )
            BulkJob.objects.filter(id=job_id).update(processed_files=F("processed_files") + 1)
        return {"file_id": file_id, "outputs": len(outputs)}

    except Exception as exc:
        logger.exception(f"Failed to process file {file_id} for bulk job {job_id}")
        with transaction.atomic():
            BulkJobFileResult.objects.filter(bulk_job_id=job_id, file_id=file_id).update(
                status="Failed", error_message=str(exc)[:4000], completed_at=timezone.now(),
            )
            BulkJob.objects.filter(id=job_id).update(failed_files=F("failed_files") + 1)
        return {"error": str(exc), "file_id": file_id}


@shared_task(bind=True)
def execute_bulk_job(self, job_id: str):
    try:
        job = BulkJob.objects.get(id=job_id)
    except BulkJob.DoesNotExist:
        logger.error(f"BulkJob {job_id} not found")
        return {"error": "Job not found"}

    job.status = "Processing"
    job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at"])

    handler = get_bulk_handler(job.job_type)
    if not handler:
        job.status = "Failed"
        job.save(update_fields=["status"])
        return {"error": f"No handler for {job.job_type}"}

    # Pre-create the output folder tree so all per-file tasks land in the same place
    if job.folder_id:
        try:
            get_bulk_output_folder(job)
        except Exception as exc:
            logger.warning(f"Failed to pre-create output folder for job {job_id}: {exc}")

    file_ids = list(
        BulkJobFileResult.objects.filter(bulk_job=job, status="Pending")
        .values_list("file_id", flat=True)
    )

    if not file_ids:
        job.status = "Completed"
        job.completed_at = timezone.now()
        job.save(update_fields=["status", "completed_at"])
        return {"message": "No files to process", "job_id": job_id}

    # batch_all handlers get ALL file_ids in a single task
    if handler.batch_all:
        logger.info(f"Batch-all handler {job.job_type}: dispatching single task "
                    f"with {len(file_ids)} files")
        task = process_batch_all_files_in_bulk.s(
            file_ids=file_ids,
            user_id=job.user_id,
            job_id=job_id,
            job_type=job.job_type,
            project_id=job.project_id,
            service_id=job.service_id,
            **(job.input_parameters or {}),
        )
        callback = finalize_bulk_job.s(str(job.id))
        result = chord([task])(callback)
        job.celery_group_id = result.id
        job.save(update_fields=["celery_group_id"])
        return {
            "job_id": job_id,
            "status": "Processing",
            "total_files": len(file_ids),
            "chunks": 1,
            "group_id": result.id,
            "mode": "batch_all",
        }

    tasks = []
    for file_id in file_ids:
        task = process_single_file_in_bulk.s(
            file_id=file_id,
            user_id=job.user_id,
            job_id=job_id,
            job_type=job.job_type,
            project_id=job.project_id,
            service_id=job.service_id,
            **(job.input_parameters or {}),
        )
        tasks.append(task)

    chunk_size = BATCH_CHUNK_SIZE
    chunks = [tasks[i:i + chunk_size] for i in range(0, len(tasks), chunk_size)]

    # Flatten chunks into a single group so chord can call finalize_bulk_job
    # when all file tasks complete
    all_tasks = []
    for chunk in chunks:
        all_tasks.extend(chunk)

    callback = finalize_bulk_job.s(str(job.id))
    result = chord(all_tasks)(callback)
    job.celery_group_id = result.id
    job.save(update_fields=["celery_group_id"])

    return {
        "job_id": job_id,
        "status": "Processing",
        "total_files": len(file_ids),
        "chunks": len(chunks),
        "group_id": result.id,
    }


@shared_task(bind=True)
def finalize_bulk_job(self, results, job_id: str):
    logger.info(f"finalize_bulk_job called for job {job_id} (received {len(results) if results else 0} results)")
    try:
        job = BulkJob.objects.get(id=job_id)
    except BulkJob.DoesNotExist:
        logger.error(f"finalize_bulk_job: job {job_id} not found")
        return {"error": "Job not found"}

    total = BulkJobFileResult.objects.filter(bulk_job=job).count()
    completed = BulkJobFileResult.objects.filter(bulk_job=job, status="Completed").count()
    failed = BulkJobFileResult.objects.filter(bulk_job=job, status="Failed").count()

    job.processed_files = completed
    job.failed_files = failed

    if failed == 0 and completed == total:
        job.status = "Completed"
    elif completed > 0 and failed > 0:
        job.status = "Partially_Completed"
    elif failed == total:
        job.status = "Failed"
    else:
        job.status = "Completed"

    job.completed_at = timezone.now()
    job.result_summary = {
        "total": total,
        "completed": completed,
        "failed": failed,
        "success_rate": round((completed / total * 100), 2) if total > 0 else 0,
    }
    job.save(update_fields=[
        "status", "processed_files", "failed_files",
        "completed_at", "result_summary",
    ])

    # Consolidated deliverables: results workbook + ZIP of output documents.
    if completed > 0:
        try:
            folder = get_bulk_output_folder(job) if job.folder_id else None
            deliverables = build_deliverables(job, get_service_display_name(job.job_type), folder)
            job.result_summary = {**(job.result_summary or {}), "deliverables": deliverables}
            job.save(update_fields=["result_summary"])
        except Exception:
            logger.exception(f"Could not build deliverables for bulk job {job_id}")

    return {
        "job_id": job_id,
        "status": job.status,
        "total": total,
        "completed": completed,
        "failed": failed,
    }


@shared_task
def cancel_bulk_job(job_id: str):
    try:
        job = BulkJob.objects.get(id=job_id)
    except BulkJob.DoesNotExist:
        return {"error": "Job not found"}

    from aiDocuMines.celery_app import app as celery_app

    if job.celery_group_id:
        try:
            celery_app.control.revoke(job.celery_group_id, terminate=True, signal="SIGTERM")
        except Exception as exc:
            logger.warning(f"Failed to revoke group {job.celery_group_id}: {exc}")

    job.status = "Cancelled"
    job.save(update_fields=["status"])

    BulkJobFileResult.objects.filter(bulk_job=job, status="Pending").update(
        status="Skipped"
    )

    return {"job_id": job_id, "status": "Cancelled"}
