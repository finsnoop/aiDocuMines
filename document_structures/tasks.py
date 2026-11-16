import time
import traceback
import logging

from celery import shared_task
from document_structures import utils, models
from core.models import File, Run, User
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
    try:
        ds_run = models.DocumentStructureRun.objects.get(id=document_structure_run_id)
        ds_run.status = "Processing"
        ds_run.save()

        file = ds_run.file
        run = ds_run.run
        user = ds_run.user

        utils.process_document_structures(
            file=file,
            run=run,
            user=user,
            partition_strategy=ds_run.partition_strategy,
            store_embeddings=store_embeddings,
            ds_run=ds_run,
        )

        ds_run.status = "Completed"
        ds_run.error_message = None
        ds_run.save()

        execution_time = time.time() - start_time

        # Generate report if requested
        if generate_report and project_id and service_id:
            try:
                # Get element count for the report
                element_count = models.DocumentElement.objects.filter(document_structure_run=ds_run).count()

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
                    user=user,
                    run=run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="document-structure-results",
                    execution_time_seconds=execution_time,
                    input_files=[{"filename": file.filename, "file_id": file.id}],
                    additional_metadata={
                        "partition_strategy": ds_run.partition_strategy,
                        "element_count": element_count,
                        "store_embeddings": store_embeddings
                    }
                )
                logger.info(f"Generated document structure report: {report_info.get('filename')}")
            except Exception as report_error:
                logger.warning(f"Failed to generate report: {report_error}")

    except Exception as e:
        logger.error(f"Error running partition task: {e}", exc_info=True)
        traceback.print_exc()
        ds_run.status = "Failed"
        ds_run.error_message = str(e)
        ds_run.save()
        raise


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

