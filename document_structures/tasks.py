import time
import traceback
import logging
import uuid

from celery import shared_task
from django.core.cache import cache
from django.db import transaction
from document_structures import utils, models
from core.models import File, Run, User
from core.processing import processing_file, source_version, unchanged_source
from core.utils import generate_and_register_service_report

logger = logging.getLogger("document_structures.tasks")


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def run_document_partition_task(
    self,
    document_structure_run_id: str,
    store_embeddings: bool = True,
    project_id: str = None,
    service_id: str = None,
    generate_report: bool = False
):
    """
    Background task that performs partitioning and extraction on a single document.
    """
    start_time = time.time()
    ds_run = None
    lock = f"document-structure:run:{document_structure_run_id}"
    token = str(uuid.uuid4())
    if not cache.add(lock, token, timeout=3600):
        return {"document_structure_run_id": str(document_structure_run_id), "status": "Processing"}
    try:
        ds_run = models.DocumentStructureRun.objects.get(id=document_structure_run_id)
        user, file = processing_file(ds_run.file_id, ds_run.user_id, permission="structure.analyze")
        if ds_run.run_id != file.run_id:
            raise ValueError("This structure run does not belong to the source document.")
        version = source_version(file)
        if ds_run.status == "Completed":
            return {"document_structure_run_id": str(ds_run.pk), "status": "Completed"}
        run = ds_run.run
        report_info = None
        # Keep extracted rows and the requested report in the same publication
        # transaction: revoked access or edited source rolls back both.
        with transaction.atomic():
            ds_run.status = "Processing"
            ds_run.save(update_fields=["status", "updated_at"])
            utils.process_document_structures(
                file=file, run=run, user=user, partition_strategy=ds_run.partition_strategy,
                store_embeddings=store_embeddings, ds_run=ds_run)
            user, file = unchanged_source(file.pk, user.pk, version, permission="structure.analyze")
            execution_time = time.time() - start_time
            if generate_report:
                element_count = models.DocumentElement.objects.filter(run=ds_run).count()
                response_data = {
                    "document_structure_run_id": str(ds_run.id),
                    "file_id": file.id,
                    "filename": file.filename,
                    "partition_strategy": ds_run.partition_strategy,
                    "status": "Completed",
                    "element_count": element_count,
                    "execution_time_seconds": round(execution_time, 2)
                }

                report_info = generate_and_register_service_report(
                    service_name="Document Structure Analysis",
                    service_id="ai-document-structure",
                    vertical="AI Services",
                    response_data=response_data,
                    user=file.user,
                    run=file.run,
                    project_id=file.project_id,
                    service_id_folder=file.service_id,
                    folder_name="document-structure-results",
                    datetime_folder=str(ds_run.pk),
                    execution_time_seconds=execution_time,
                    input_files=[{"filename": file.filename, "file_id": file.id}],
                    additional_metadata={
                        "partition_strategy": ds_run.partition_strategy,
                        "element_count": element_count,
                        "store_embeddings": store_embeddings
                    },
                    source_file=file, actor=user, expected_source_version=version,
                    permission="structure.analyze"
                )
                logger.info(f"Generated document structure report: {report_info.get('filename')}")
            ds_run.status = "Completed"
            ds_run.error_message = None
            ds_run.save(update_fields=["status", "error_message", "updated_at"])
        return {"document_structure_run_id": str(ds_run.pk), "status": "Completed", "report": report_info}

    except Exception as e:
        logger.error(f"Error running partition task: {e}", exc_info=True)
        traceback.print_exc()
        if ds_run is not None:
            ds_run.status = "Failed"
            ds_run.error_message = f"Document structure analysis failed ({type(e).__name__}). Submit it again."
            ds_run.save()
        raise
    finally:
        if cache.get(lock) == token:
            cache.delete(lock)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def run_document_comparison_task(self, comparison_id: str):
    """
    Background task to compare two document structure runs.
    """

    try:
        comparison = models.DocumentComparison.objects.get(id=comparison_id)
        comparison.status = "Processing"
        comparison.save()

        run1 = comparison.run_1
        run2 = comparison.run_2

        # Call optimized utils function
        comparison_obj = utils.compare_document_runs(run1, run2)

        comparison.lexical_similarity = comparison_obj.lexical_similarity
        comparison.semantic_similarity = comparison_obj.semantic_similarity
        comparison.deviation_report = comparison_obj.deviation_report or {}
        comparison.status = "Completed"
        comparison.save()

        logger.warning(f"Comparison {comparison.id} complete.")

    except Exception as e:
        logger.error(f"Error running comparison task: {e}", exc_info=True)
        comparison.status = "Failed"
        comparison.save()
        raise

