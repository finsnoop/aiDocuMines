from celery import shared_task
import os
import json
import logging
from django.conf import settings
from oauth2_provider.models import Application
from django.utils.timezone import make_aware
from datetime import datetime
import asyncio

from pytz import timezone, UTC
from celery import group
from .models import File, Metadata, Storage, Run, EndpointResponseTable
from .utils import save_uploaded_file, extract_metadata, calculate_md5, convert_pdf_date, str_to_bool
from .serializers import MetadataSerializer
from document_operations.utils import register_file_folder_link

logger = logging.getLogger(__name__)

from .utils import extract_metadata


def _store_metadata(file, defaults):
    """Serialize metadata publication per source without deleting older rows."""
    from django.db import transaction
    with transaction.atomic():
        File.objects.select_for_update().get(pk=file.pk)
        existing = Metadata.objects.filter(file=file).order_by("-updated_at", "-metadata_id").first()
        # Native custom properties are untrusted document data. Keep them
        # separate from explicit analysis results (for example risk scores).
        previous = {}
        if existing is not None and existing.custom_metadata:
            try:
                previous = json.loads(existing.custom_metadata)
            except (TypeError, ValueError):
                pass
        if not isinstance(previous, dict):
            previous = {}
        try:
            native = json.loads(defaults.get("custom_metadata") or "null")
        except (TypeError, ValueError):
            native = None
        previous["document_properties"] = native
        defaults["custom_metadata"] = json.dumps(previous, default=str)
        if existing is None:
            return Metadata.objects.create(file=file, **defaults), True
        for field, value in defaults.items():
            setattr(existing, field, value)
        existing.save()
        return existing, False


@shared_task(bind=True)
def process_metadata(self, file_id, run_id, requested_by_id=None):
    """Celery task to extract metadata asynchronously and store in DB."""
    try:
        file_instance = File.objects.get(id=file_id)
        run = Run.objects.get(run_id=run_id)
        from core.access import get_file_for_user, user_has_perm
        from django.contrib.auth import get_user_model
        actor_id = requested_by_id or run.user_id
        if requested_by_id is None and run.user_id != file_instance.user_id:
            raise ValueError("Metadata job does not belong to the source owner.")
        actor = get_user_model().objects.get(pk=actor_id, is_active=True)
        if not user_has_perm(actor, "document.view"):
            raise ValueError("Your role cannot inspect document metadata.")
        file_instance = get_file_for_user(actor, file_id, "view")
        from core.processing import source_version
        version = source_version(file_instance)

        logger.warning(f"**Processing metadata for file_id: {file_id}, run_id: {run_id}**")

        # Extract metadata
        metadata_data = extract_metadata(file_instance)

        if not metadata_data:
            raise ValueError("Metadata extraction failed.")

        logger.info("Extracted metadata for file %s.", file_id)

        # Normalize all boolean fields
        boolean_fields = [
            "is_encrypted", "encrypted", "optimized"
        ]
        for key in boolean_fields:
            if key in metadata_data:
                metadata_data[key] = str_to_bool(metadata_data[key])

        
        # Convert Dates to Django `datetime` format (Ensures Correct Storage)
        #def parse_date(date_str):
        #    """Converts a string date into a Django-aware datetime object."""
        #    if not date_str:
        #        return None
        #    try:
        #        dt = datetime.fromisoformat(date_str)  # Convert from ISO 8601
        #        return make_aware(dt)  # Ensure it's timezone-aware
        #    except ValueError:
        #        logger.warning(f"Failed to parse date: {date_str}")
        #        return None
        


        def parse_date(date_str):
            """Converts a string date into a Django-aware datetime object."""
            if not date_str:
                return None
            from django.utils.timezone import is_naive
            if isinstance(date_str, datetime):
                return make_aware(date_str) if is_naive(date_str) else date_str
            if not isinstance(date_str, str):
                return None
            try:
                # Primary: ISO format
                dt = datetime.fromisoformat(date_str)
                return make_aware(dt) if is_naive(dt) else dt
            except (ValueError, TypeError):
                pass

            try:
                # Fallback 1: "Fri Jan 24 03:38:03 2025 CST"
                dt = datetime.strptime(date_str, "%a %b %d %H:%M:%S %Y %Z")
                return dt.replace(tzinfo=UTC)  # or use pytz.timezone("US/Central") if you want CST
            except ValueError:
                pass

            try:
                # Fallback 2: "/creationdate": "D:20250124093803+00'00'"
                if date_str.startswith("D:"):
                    cleaned = date_str[2:].split("+")[0]
                    dt = datetime.strptime(cleaned, "%Y%m%d%H%M%S")
                    return make_aware(dt)
            except Exception:
                pass

            logger.warning(f"Failed to parse date: {date_str}")
            return None



        creationdate = parse_date(metadata_data.get("creationdate", None))
        moddate = parse_date(metadata_data.get("moddate", None))
        fresh = get_file_for_user(actor, file_id, "view")
        if source_version(fresh) != version:
            raise ValueError("Document changed during metadata extraction. Submit it again.")

        # Ensure Metadata is saved correctly
        metadata, created = _store_metadata(
            file_instance,
            defaults = {
                "storage": file_instance.storage,  # Link Storage
                "file_size": metadata_data.get("file_size", None),
                "format": metadata_data.get("format", None),
                "md5_hash": metadata_data.get("md5_hash", None),
                "title": metadata_data.get("title", None),
                "author": metadata_data.get("author", None),
                "subject": metadata_data.get("subject", None),
                "keywords": metadata_data.get("keywords", None),
                "creator": metadata_data.get("creator", None),
                "producer": metadata_data.get("producer", None),
                "last_modified_by": metadata_data.get("last_modified_by"),
                "category": metadata_data.get("category"),
                "content_status": metadata_data.get("content_status"),
                "revision": metadata_data.get("revision"),
                "custom_metadata": json.dumps(metadata_data.get("custom_metadata"), default=str),
                "creationdate": creationdate, # metadata_data.get("creationdate", None),  # Corrected from creation_date
                "moddate": moddate, # metadata_data.get("moddate", None),  # Corrected from modDate
                "trapped": metadata_data.get("trapped", None),
                "encryption": metadata_data.get("encryption", None),
                "page_count": metadata_data.get("page_count", 0),
                "is_encrypted": metadata_data.get("is_encrypted", False),
                "fonts": metadata_data.get("fonts", None),
                "page_rotation": metadata_data.get("page_rotation", 0),
                "pdfminer_info": metadata_data.get("pdfminer_info", None),
                "metadata_stream": metadata_data.get("metadata_stream", None),
                "tagged": metadata_data.get("tagged", None),
                "userproperties": metadata_data.get("userproperties", None),  # Corrected from user_properties
                "suspects": metadata_data.get("suspects", None),
                "form": metadata_data.get("form", None),
                "javascript": metadata_data.get("javascript", None),
                "pages": metadata_data.get("pages", None),
                "encrypted": metadata_data.get("encrypted", False),
                "page_size": metadata_data.get("page_size", None),
                "optimized": metadata_data.get("optimized", False),
                "pdf_version": metadata_data.get("pdf_version", None),
                "word_count": metadata_data.get("word_count", 0),
            }
        )
        logger.info(f"Metadata saved: {metadata}")



        # Save task result in EndpointResponseTable
        EndpointResponseTable.objects.update_or_create(
            run=run,
            client=file_instance.user,
            endpoint_name="MetadataView",
            defaults={
                "status": "Completed",
                "response_data": {
                    "metadata_id": metadata.metadata_id,
                    "file_id": file_instance.id,
                    "filename": file_instance.filename,
                    "md5_hash": metadata.md5_hash,
                    "format": metadata.format,
                    "pdf_version": metadata.pdf_version,
                    "page_count": metadata.page_count,
                    "fonts": metadata.fonts,
                    "created_at": str(metadata.created_at),
                },
            },
        )


        # Update run status
        run.status = "Completed"
        run.save(update_fields=["status"])

        return {
            "message": "Metadata extraction completed.",
            "metadata_id": metadata.metadata_id,
        }

    except File.DoesNotExist:
        logger.error("File does not exist.")
        return {"error": "File does not exist."}
    except Run.DoesNotExist:
        logger.error(f"Run ID {run_id} does not exist.")
        return {"error": f"Run ID {run_id} does not exist."}
    except Exception as e:
        logger.error(f"Metadata extraction failed: {str(e)}")
        if 'run' in locals():
            run.status = "Failed"
            run.save(update_fields=["status", "updated_at"])
            EndpointResponseTable.objects.update_or_create(run=run, client=run.user, endpoint_name="MetadataView",
                defaults={"status": "Failed", "response_data": {"file_id": file_id, "error": str(e)}})
        return {"error": str(e)}




@shared_task(bind=True)
def process_bulk_metadata(self, run_id):
    """
    Process metadata for all files belonging to the given run_id in parallel.
    Aggregates individual tasks for Celery to process concurrently.
    """
    try:
        run = Run.objects.get(run_id=run_id)
        files = File.objects.filter(run=run)

        if not files.exists():
            logger.warning(f"No files found for run_id: {run_id}")
            return {"warning": "No files found for this run_id."}

        logger.info(f"Triggering bulk metadata extraction for run_id: {run_id} with {files.count()} files")

        # Launch a group of tasks concurrently
        metadata_tasks = group(process_metadata.s(file.id, run_id) for file in files)
        result = metadata_tasks.apply_async()

        return {"message": "Bulk metadata extraction triggered.", "task_ids": result.id}

    except Run.DoesNotExist:
        logger.error(f"Run ID {run_id} not found for bulk metadata processing.")
        return {"error": f"Run ID {run_id} not found."}
    except Exception as e:
        logger.error(f"Bulk metadata processing failed: {str(e)}")
        return {"error": str(e)}


@shared_task
def process_file(file_id):
    """
    Celery task to process a file and update the response table.
    """
    try:
        file_instance = File.objects.get(id=file_id)
        run = file_instance.run

        # Get client_id from OAuth2 Application model
        application = Application.objects.get(user=run.user)
        client_id = application.client_id  # Correct way to get client_id

        # Step 1: Compute MD5 Hash
        file_hash = asyncio.run(calculate_md5(file_instance.filepath))

        # Step 2: Update File Instance with Hash
        file_instance.md5_hash = file_hash
        file_instance.save(update_fields=["md5_hash"])

        # Step 3: Store or update response in EndpointResponseTable
        endpoint_response, created = EndpointResponseTable.objects.update_or_create(
            run=run,
            endpoint_name="FileUploadView",
            defaults={
                "status": "Completed",
                "response_data": {
                    "client_id": client_id,
                    "project_id": file_instance.project_id,
                    "service_id": file_instance.service_id,
                    "user": file_instance.user.username,
                    "files": [
                        {
                            "file_id": file_instance.id,
                            "filename": file_instance.filename,
                            "filepath": file_instance.filepath,
                            "md5_hash": file_hash,
                        }
                    ]
                }
            }
        )

        logger.info(f"File processing completed for file_id: {file_id}")

    except Exception as e:
        # Handle failures and store error message
        endpoint_response, created = EndpointResponseTable.objects.update_or_create(
            run=run,
            endpoint_name="FileUploadView",
            defaults={
                "status": "Failed",
                "response_data": {"error": str(e)}
            }
        )
        logger.error(f"File processing failed for file_id: {file_id} - Error: {str(e)}")





@shared_task
def extract_document_text_task(file_id):
    """
    Extracts plain-text content for an uploaded file and drives the
    File.status lifecycle (Pending -> Processing -> Completed/Failed).

    This task is queued for every upload, so it is the single place where
    the per-file processing status is finalised. Without it, files would
    stay "Pending" forever.
    """
    from document_search.text_policy import TextPreparationBusy, shared_file_text

    try:
        file_obj = File.objects.get(id=file_id)
    except File.DoesNotExist:
        logger.warning("extract_document_text_task: file %s does not exist.", file_id)
        return

    # Mark the file as actively processing as soon as work starts.
    if file_obj.status in ("Pending", "Processing"):
        file_obj.status = "Processing"
        file_obj.save(update_fields=["status", "updated_at"])

    try:
        try:
            text = shared_file_text(file_obj, allow_ocr=False)
        except TextPreparationBusy:
            text = ""  # a downstream indexer is preparing it; upload itself is complete
        if not text:
            text = File.objects.filter(pk=file_id).values_list("content", flat=True).first() or ""
        file_obj.content = text or ""
        file_obj.status = "Completed"
        fields = ["status", "updated_at"]
        if text:
            fields.append("content")
        file_obj.save(update_fields=fields)
        logger.info("Text extraction completed for file_id: %s (%s chars)",
                    file_id, len(file_obj.content))
    except Exception as exc:
        logger.exception("Text extraction failed for file_id %s: %s", file_id, exc)
        File.objects.filter(id=file_id).update(status="Failed")
        raise



# @shared_task
# async def async_process_upload(uploaded_file, user_id, project_id, service_id, run_id):
    # """
    # Asynchronous file upload processing task.
    # """
    # try:
        # file_details = await save_uploaded_file(uploaded_file, user_id, project_id, service_id, run_id)
# 
        # Create File instance in DB
        # file_instance = File.objects.create(
            # filename=file_details["filename"],
            # filepath=file_details["file_path"],
            # file_size=file_details["file_size"],
            # file_type=file_details["file_type"],
            # md5_hash=file_details["md5_hash"],
            # run=Run.objects.get(run_id=run_id),
            # user_id=user_id,
            # client_id=user_id,  # Assuming client_id is same as user_id
            # project_id=project_id,
            # service_id=service_id,
        # )
# 
        # logger.info(f"File upload completed: {file_instance.filename} - ID: {file_instance.id}")
# 
        # return {"message": "File uploaded successfully", "file_id": file_instance.id}
# Import bulk processing tasks so Celery discovers them (file is tasks_bulk.py, not tasks.py)
from core.tasks_bulk import execute_bulk_job, process_single_file_in_bulk, cancel_bulk_job, finalize_bulk_job  # noqa: F401
