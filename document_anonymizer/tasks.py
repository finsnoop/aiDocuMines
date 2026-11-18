import os
import json
import logging
import uuid
from celery import shared_task
from django.db import transaction
from django.shortcuts import get_object_or_404
from core.models import File
from document_anonymizer.models import AnonymizationRun, Anonymize, DeAnonymize
from document_anonymizer.utils import AnonymizationService, generate_anonymized_html
from core.utils import register_generated_file
from document_anonymizer.utils import compute_global_anonymization_stats
from document_anonymizer.models import AnonymizationStats
from django.contrib.auth import get_user_model
# from platform_data_insights.utils import calculate_anonymization_insights
from document_anonymizer.utils import calculate_anonymization_insights, get_shared_anonymization_service


User = get_user_model()


logger = logging.getLogger(__name__)
# service = AnonymizationService()
from django.utils.functional import SimpleLazyObject
service = SimpleLazyObject(get_shared_anonymization_service)

def submit_anonymization(file, actor, file_type="plain"):
    from core.processing import processing_file, source_version
    from django.core.cache import cache
    file_type = (file_type or "plain").strip().lower()
    if file_type in {"txt", "text"}:
        file_type = "plain"
    if file_type not in {"plain", "structured"}:
        raise ValueError("file_type must be plain or structured.")
    with transaction.atomic():
        File.objects.select_for_update().get(pk=file.pk)
        actor, file = processing_file(file.pk, actor.pk, permission="anonymization.submit")
        active = Anonymize.objects.filter(original_file=file, file_type=file_type, is_active=True).select_related("run").first()
        if active and active.run:
            if active.run.status in {"Pending", "Processing"}:
                return active.run, False
            if active.status == "Completed" and cache.get(f"explicit-anonymization-source:{active.run_id}") == source_version(file):
                return active.run, False
            active.is_active = False
            active.save(update_fields=["is_active"])
        run = AnonymizationRun.objects.create(project_id=file.project_id, service_id=file.service_id,
            client=actor.client, client_name=actor.username or actor.email, status="Pending",
            anonymization_type="Presidio-Spacy")
        Anonymize.objects.create(original_file=file, run=run, file_type=file_type, is_active=True, status="Processing")
        transaction.on_commit(lambda: anonymize_document_task.delay(file.pk, file_type, str(run.pk), user_id=actor.pk))
        return run, True


@shared_task(time_limit=1800, soft_time_limit=1740)
def anonymize_document_task(file_id, file_type="plain", run_id=None, user_id=None):
    """Publish redacted outputs only after source and requester revalidation."""
    from django.core.cache import cache
    from core.processing import processing_file, source_version, unchanged_source
    from document_search.text_policy import shared_file_text
    run = AnonymizationRun.objects.get(pk=run_id) if run_id else None
    entry = None
    try:
        file_type = (file_type or "plain").lower().strip()
        if file_type in {"txt", "text"}:
            file_type = "plain"
        if file_type not in {"plain", "structured"}:
            raise ValueError("Invalid anonymization output type.")
        actor, file = processing_file(file_id, user_id, run=run, permission="anonymization.submit")
        if run is None:
            run = AnonymizationRun.objects.create(project_id=file.project_id, service_id=file.service_id,
                client=actor.client, client_name=actor.username or actor.email, status="Processing")
        version = source_version(file)
        run.status = "Processing"
        run.save(update_fields=["status", "updated_at"])
        # Native-first preparation, with conditional PDF OCR only because this
        # requested anonymization needs text. Image OCR remains a separate action.
        text = shared_file_text(file, allow_ocr=True)
        if not text.strip():
            raise ValueError("No readable source text. Run OCR for an image or review the PDF.")
        structured_text, _, blocks = service.extract_structured_text(file.filepath)
        if not structured_text or not blocks:
            blocks = [{"element_id": "text-0", "type": "Text", "text": text}]
        updated_blocks, combined, presidio, spacy = [], {}, {}, {}
        for source_block in blocks:
            block = dict(source_block)
            masked, mapping, presidio_map, spacy_map = service.anonymize_text(block.get("text", ""))
            block["text"] = masked
            updated_blocks.append(block)
            combined.update(mapping)
            presidio.update(presidio_map)
            spacy.update(spacy_map)
        masked_text = "\n\n".join(block["text"] for block in updated_blocks)
        risk = service.compute_risk_score(text, presidio, spacy)
        output_dir = os.path.join(os.path.dirname(file.filepath), "anonymized", str(run.pk))
        os.makedirs(output_dir, exist_ok=True)
        txt_path = os.path.join(output_dir, "anonymized.txt")
        html_path = os.path.join(output_dir, "anonymized.html")
        structured_path = os.path.join(output_dir, "anonymized_structured.json")
        mapping_path = os.path.join(output_dir, "private_entity_mapping.json")
        with open(txt_path, "w", encoding="utf-8") as stream:
            stream.write(masked_text)
        with open(html_path, "w", encoding="utf-8") as stream:
            stream.write(generate_anonymized_html(masked_text, {}))
        with open(structured_path, "w", encoding="utf-8") as stream:
            json.dump(updated_blocks, stream, ensure_ascii=False, indent=2)
        # Reversal mappings are private processing data. They are never indexed
        # or registered as an anonymized document that a user might share.
        with open(mapping_path, "w", encoding="utf-8") as stream:
            json.dump(combined, stream, ensure_ascii=False, indent=2)
        os.chmod(mapping_path, 0o600)
        with transaction.atomic():
            actor, file = unchanged_source(file.pk, actor.pk, version, run=run, permission="anonymization.submit")
            File.objects.select_for_update().get(pk=file.pk)
            entry = Anonymize.objects.filter(original_file=file, file_type=file_type, is_active=True).first()
            if entry and entry.run_id != run.pk:
                entry.is_active = False
                entry.save(update_fields=["is_active"])
                entry = None
            if entry is None:
                entry = Anonymize(original_file=file, run=run, file_type=file_type, is_active=True)
            entry.anonymized_filepath = txt_path
            entry.anonymized_html_filepath = html_path
            entry.entity_mapping_filepath = mapping_path
            entry.anonymized_structured_filepath = structured_path if file_type == "structured" else None
            entry.anonymized_structured_txt_filepath = txt_path if file_type == "structured" else None
            entry.anonymized_structured_html_filepath = html_path if file_type == "structured" else None
            entry.presidio_masking_map = presidio
            entry.spacy_masking_map = spacy
            entry.risk_score, entry.risk_level = risk["risk_score"], risk["risk_level"]
            entry.risk_breakdown = risk["breakdown"]
            entry.risk_contributions = risk.get("contributions")
            entry.status = "Completed"
            entry.save()
            paths = [txt_path, html_path] + ([structured_path] if file_type == "structured" else [])
            outputs = [register_generated_file(file_path=path, user=file.user, run=file.run,
                project_id=file.project_id, service_id=file.service_id, folder_name="anonymized",
                source_file=file, actor=actor, expected_source_version=version, permission="anonymization.submit") for path in paths]
            unchanged_source(file.pk, actor.pk, version, run=run, permission="anonymization.submit")
            run.status, run.error_message = "Completed", ""
            run.save(update_fields=["status", "error_message", "updated_at"])
        try:
            cache.set(f"explicit-anonymization-source:{run.pk}", version, timeout=86400)
        except Exception:
            logger.warning("Could not cache completed source version for %s", run.pk)
        from core.utils import get_logical_path
        return {"file_id": file.pk, "anonymization_run_id": str(run.pk), "file_type": file_type,
            "status": "Completed", "registered_outputs": [{"file_id": f.pk, "filename": f.filename,
                "path": get_logical_path(f.filepath, f.filename)} for f in outputs]}
    except Exception:
        logger.exception("Requested anonymization failed: %s", run_id)
        error = "Anonymization failed. Check source text and processing service, then retry."
        if run is not None:
            run.status, run.error_message = "Failed", error
            run.save(update_fields=["status", "error_message", "updated_at"])
            Anonymize.objects.filter(run=run).update(status="Failed")
        return {"status": "Failed", "error": error, "file_id": file_id,
                "anonymization_run_id": str(run.pk) if run else None}


@shared_task
def deanonymize_document_task(file_id, user_id=None):
    logger.info(f"Starting de-anonymization for file_id={file_id}")

    from core.processing import processing_file, source_version, unchanged_source
    actor, source = processing_file(file_id, user_id, permission="anonymization.submit")
    version = source_version(source)
    instance = get_object_or_404(Anonymize, original_file=source, is_active=True, file_type="plain")

    if not os.path.exists(instance.anonymized_filepath):
        return {"error": "Masked file not found", "file_id": file_id}

    with open(instance.anonymized_filepath, "r", encoding="utf-8") as f:
        masked_text = f.read()

    final_text = service.deanonymize_pipeline(
        masked_text,
        spacy_mapping=instance.spacy_masking_map or {},
        presidio_mapping=instance.presidio_masking_map or {}
    )

    deanonymized_dir = os.path.join(os.path.dirname(instance.original_file.filepath), "deanonymized")
    os.makedirs(deanonymized_dir, exist_ok=True)
    txt_path = os.path.join(deanonymized_dir, f"deanonymized_{uuid.uuid4()}.txt")

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(final_text)

    with transaction.atomic():
        actor, source = unchanged_source(source.pk, actor.pk, version, permission="anonymization.submit")
        registered = register_generated_file(file_path=txt_path, user=source.user, run=source.run,
            project_id=source.project_id, service_id=source.service_id, folder_name="deanonymized", source_file=source, actor=actor, expected_source_version=version, permission="anonymization.submit")
        DeAnonymize.objects.create(
            file=instance.original_file,
            unmasked_text=final_text,
            unmasked_filepath=txt_path,
            presidio_masking_map=instance.presidio_masking_map,
            spacy_masking_map=instance.spacy_masking_map,
            status="Completed"
        )

    logger.info(f"De-anonymization complete for file_id={file_id}")
    return {"file_id": file_id, "deanonymized_txt": txt_path, "status": "Completed"}


@shared_task
def compute_risk_score_task(file_id, user_id=None):
    """An explicit privacy assessment can run without creating anonymized files."""
    from core.processing import processing_file, source_version, unchanged_source
    from document_search.text_policy import shared_file_text
    from core.models import Metadata
    actor, file = processing_file(file_id, user_id, permission="anonymization.submit")
    version = source_version(file)
    text = shared_file_text(file, allow_ocr=True)
    if not text.strip():
        return {"error": "No readable source text. Run OCR for an image or review the PDF.", "status": "Failed"}
    _, _, presidio, spacy = service.anonymize_text(text)
    result = service.compute_risk_score(text, presidio, spacy)
    with transaction.atomic():
        actor, file = unchanged_source(file.pk, actor.pk, version, permission="anonymization.submit")
        File.objects.select_for_update().get(pk=file.pk)
        metadata = Metadata.objects.filter(file=file).order_by("-metadata_id").first()
        if metadata is None:
            metadata = Metadata(file=file)
        try:
            custom = json.loads(metadata.custom_metadata or "{}")
        except (TypeError, ValueError):
            custom = {}
        if not isinstance(custom, dict):
            custom = {"extracted": custom}
        custom["privacy_risk"] = {**result, "requested_by": actor.pk, "source_version": list(version)}
        metadata.custom_metadata = json.dumps(custom)
        metadata.save()
        Anonymize.objects.filter(original_file=file, is_active=True, status="Completed").update(
            risk_score=result["risk_score"], risk_level=result["risk_level"], risk_breakdown=result["breakdown"],
            risk_contributions=result.get("contributions"))
    return {"file_id": file.pk, "status": "Completed", **result}


@shared_task
def compute_anonymization_stats_task(
    user_id,
    client_name=None,
    project_id=None,
    service_id=None,
    date_from=None,
    date_to=None
):
    logger.info(f"Computing anonymization stats...")

    result = compute_global_anonymization_stats(
        user_id=user_id,
        client_name=client_name,
        project_id=project_id,
        service_id=service_id,
        date_from=date_from,
        date_to=date_to,
    )

    # Save to DB
    stats_record = AnonymizationStats.objects.create(
        client_name=client_name,
        project_id=project_id,
        service_id=service_id,
        files_with_entities=result["files_with_entities"],
        files_without_entities=result["files_without_entities"],
        total_entities_anonymized=result["total_entities_anonymized"],
        entity_type_breakdown=result["entity_type_breakdown"],
    )

    logger.info(f"Stats saved to DB (id={stats_record.id})")

    return {
        "stats_id": str(stats_record.id),
        **result,
    }




@shared_task
def generate_anonymization_insights_task(user_id):
    """
    Celery task to compute anonymization insights for a single user.
    """
    logger.info(f"Generating anonymization insights for user_id={user_id}")

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.error(f"User not found: {user_id}")
        return {"error": f"User {user_id} not found"}

    insights = calculate_anonymization_insights(user)

    logger.info(f"Anonymization insights generated for user_id={user_id}")
    return insights



@shared_task(name="document_anonymizer.cache_supported_entities", ignore_result=True, soft_time_limit=600)
def cache_supported_entities():
    """Compute the installed models' entity labels once and share them with every web replica (7 days)."""
    from django.core.cache import cache
    from document_anonymizer.utils import SUPPORTED_ENTITIES_CACHE_KEY, list_supported_entities_runtime
    if not cache.add(SUPPORTED_ENTITIES_CACHE_KEY + ":lock", 1, timeout=900):
        return   # another worker is already computing it
    data = list_supported_entities_runtime()
    if data.get("presidio") or data.get("spacy"):
        data["meta"]["source"] = "runtime"
        cache.set(SUPPORTED_ENTITIES_CACHE_KEY, data, 7 * 24 * 3600)
