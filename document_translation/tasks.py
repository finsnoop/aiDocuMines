import logging
import os
from pathlib import Path
from celery import shared_task
from django.core.cache import cache
from django.db import transaction
from django.utils.functional import SimpleLazyObject
from core.processing import processing_file, source_version, unchanged_source
from core.utils import get_logical_path, register_generated_file
from document_translation.models import TranslationFile, TranslationRun, TranslationStorage
from document_translation.utils import AzureBlobService, TranslationService

logger = logging.getLogger(__name__)
translation_service = SimpleLazyObject(TranslationService)
azure_service = SimpleLazyObject(AzureBlobService)


def _registered(source, actor, path, language, version):
    return register_generated_file(
        file_path=path, user=source.user, run=source.run,
        project_id=source.project_id, service_id=source.service_id,
        folder_name=os.path.join("translations", language), source_file=source, actor=actor,
        expected_source_version=version, permission="translation.submit")


def _result(run, source, registered, path):
    return {"translation_run_id": str(run.pk), "file_id": source.pk, "status": "Completed",
            "translated_filepath": get_logical_path(path),
            "registered_outputs": [{"filename": registered.filename, "file_id": registered.pk,
                                    "path": get_logical_path(registered.filepath, registered.filename)}]}


@shared_task
def translate_document_task(file_id, translation_run_id, from_language, to_language, requested_by_id=None):
    """Publish requested translation only after fresh source and actor checks."""
    run = TranslationRun.objects.filter(pk=translation_run_id).first()
    if run is None:
        return {"error": "Translation run not found.", "status": "Failed"}
    lock = f"translation:run:{run.pk}"
    if not cache.add(lock, "running", timeout=3600):
        return {"translation_run_id": str(run.pk), "status": "Processing"}
    try:
        actor, source = processing_file(file_id, requested_by_id, run=run, permission="translation.submit")
        version = source_version(source)
        if run.from_language != from_language or run.to_language != to_language:
            raise ValueError("Requested languages do not match this translation run.")
        existing = TranslationFile.objects.filter(run=run, original_file=source, status="Completed").first()
        if existing and existing.translated_filepath and os.path.isfile(existing.translated_filepath):
            registered = _registered(source, actor, existing.translated_filepath, to_language, version)
            return _result(run, source, registered, existing.translated_filepath)
        TranslationFile.objects.update_or_create(run=run, original_file=source, defaults={
            "original_filepath": source.filepath, "status": "Processing"})
        run.status = "Translating"
        run.error_message = None
        run.save(update_fields=["status", "error_message", "updated_at"])
        result = translation_service.translate_file(source.pk, run.pk, from_language, to_language)
        path = result.get("translated_file")
        if not path or not os.path.isfile(path) or not os.path.getsize(path):
            raise ValueError("Translation returned no readable output file.")
        output_root = Path(source.filepath).resolve().parent / "translations"
        if not Path(path).resolve().is_relative_to(output_root):
            raise ValueError("Translation output escaped its processing folder.")
        actor, source = unchanged_source(source.pk, actor.pk, version, run=run, permission="translation.submit")
        with transaction.atomic():
            registered = _registered(source, actor, path, to_language, version)
            TranslationFile.objects.update_or_create(run=run, original_file=source, defaults={
                "original_filepath": source.filepath, "translated_filepath": path, "status": "Completed"})
            TranslationStorage.objects.update_or_create(run=run, defaults={
                "upload_storage_location": source.filepath, "translated_storage_location": path})
            run.status = "Completed"
            run.save(update_fields=["status", "updated_at"])
        return _result(run, source, registered, path)
    except Exception as exc:
        logger.warning("Translation run %s failed: %s", run.pk, type(exc).__name__)
        message = ("Document translation is not configured for this deployment."
                   if "not configured" in str(exc).lower() else
                   f"Translation failed ({type(exc).__name__}). Check the service and submit again.")
        run.status = "Failed"
        run.error_message = message
        run.save(update_fields=["status", "error_message", "updated_at"])
        TranslationFile.objects.filter(run=run, original_file_id=file_id).update(status="Failed")
        return {"translation_run_id": str(run.pk), "file_id": file_id, "status": "Failed", "error": message}
    finally:
        cache.delete(lock)


@shared_task
def check_translation_status_task(translation_run_id):
    run = TranslationRun.objects.get(pk=translation_run_id)
    return {"translation_run_id": str(run.pk), "status": run.status,
            "from_language": run.from_language, "to_language": run.to_language,
            "error_message": run.error_message}


@shared_task
def download_translated_file_task(file_id):
    """Resolve saved output without inventing a cloud container or UUID path."""
    translated = TranslationFile.objects.filter(original_file_id=file_id, status="Completed").order_by("-created_at").first()
    if translated is None or not translated.translated_filepath or not os.path.isfile(translated.translated_filepath):
        return {"error": "Translated file not found.", "file_id": file_id}
    return {"file_id": file_id, "translated_filepath": translated.translated_filepath, "status": "Ready for download"}
