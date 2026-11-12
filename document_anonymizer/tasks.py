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
service = get_shared_anonymization_service() 

@shared_task
def anonymize_document_task(file_id, file_type="plain", run_id=None):
    logger.info(f"🔄 Starting anonymization for file_id={file_id} (type={file_type})")
    file_entry = get_object_or_404(File, id=file_id)

    def mark_run_failed(msg: str):
        logger.error(f"❌ {msg}")
        if run_id:
            try:
                run = AnonymizationRun.objects.get(id=run_id)
                run.status = "Failed"
                # only if your model has error_message; if not, drop this line
                if hasattr(run, "error_message"):
                    run.error_message = msg
                run.save(update_fields=["status"] + (["error_message"] if hasattr(run, "error_message") else []))
            except AnonymizationRun.DoesNotExist:
                logger.error(f"❌ AnonymizationRun {run_id} not found to mark as Failed")

    if not os.path.exists(file_entry.filepath):
        logger.error(f"❌ File not found: {file_entry.filepath}")
        return {"error": "File not found", "file_id": file_id}


    # ✅ Normalize file_type
    file_type = (file_type or "plain").lower().strip()
    if file_type in {"txt", "text"}:
        file_type = "plain"


    # ✅ EARLY EXIT: Already anonymized
    existing = Anonymize.objects.filter(original_file=file_entry, file_type=file_type, is_active=True).first()
    if existing:
        logger.info(f"⚠️ Skipping duplicate anonymization. File ID {file_id} already has active anonymized output (ID: {existing.id})")
        return {
            "status": "Already Anonymized",
            "anonymized_id": str(existing.id),
            "file_id": file_id,
            "message": "An active anonymization already exists for this file."
        }

    anonymized_dir = os.path.join(os.path.dirname(file_entry.filepath), "anonymized")
    os.makedirs(anonymized_dir, exist_ok=True)

    base_filename = f"anonymized_{uuid.uuid4()}"
    txt_path = os.path.join(anonymized_dir, f"{base_filename}.txt")
    json_path = os.path.join(anonymized_dir, f"{base_filename}.json")
    html_path = os.path.join(anonymized_dir, f"{base_filename}.html")
    structured_json_path = os.path.join(anonymized_dir, f"{base_filename}_structured.json")
    structured_txt_path = os.path.join(anonymized_dir, f"{base_filename}_structured.txt")
    structured_html_path = os.path.join(anonymized_dir, f"{base_filename}_structured.html")

    # ✅ Structured extraction (PDF/DOCX only)
    '''
    try:
        structured_text, _, elements_json = service.extract_structured_text(file_entry.filepath)
    except Exception as e:
        mark_run_failed(f"Structured text extraction crashed: {e}")
        return {"error": "Structured extraction crashed", "file_id": file_id}

    if not structured_text:
        mark_run_failed("Empty structured content (unsupported type or zero text).")
        return {"error": "Empty structured content", "file_id": file_id}
    '''

    # after: graceful fallback to raw extraction
    structured_text, elements, elements_json = service.extract_structured_text(file_entry.filepath)
    raw_text = None

    if not structured_text:
        raw_text, _, _ = service.extract_text_from_file(file_entry.filepath)
        if not raw_text:
            mark_run_failed("No extractable content.")
            return {"error": "No extractable content", "file_id": file_id}
        # make a single synthetic block so the rest of the pipeline can run uniformly
        elements_json = [{"element_id": "raw-0", "type": "Text", "text": raw_text}]
        structured_text = raw_text                  # ✅ ensure downstream writes/HTML have text

    #try:
    #    structured_text, _, elements_json = service.extract_structured_text(file_entry.filepath)
    #except Exception as e:
    #    logger.exception(f"❌ Structured text extraction failed: {e}")
    #    return {"error": "Structured extraction crashed", "file_id": file_id}

    # if not structured_text:
    #    return {"error": "Empty structured content", "file_id": file_id}

    updated_blocks = []
    global_combined_map = {}
    global_presidio_map = {}
    global_spacy_map = {}

    for block in elements_json:
        original_text = block.get("text", "")
        masked_text, combined_map, presidio_map, spacy_map = service.anonymize_text(original_text)
        block["text"] = masked_text
        updated_blocks.append(block)
        global_combined_map.update(combined_map)
        global_presidio_map.update(presidio_map)
        global_spacy_map.update(spacy_map)

    final_masked_doc = "\n\n".join(block["text"] for block in updated_blocks)

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(final_masked_doc)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(global_combined_map, f, indent=4)

    ''' 
    with open(structured_json_path, "w", encoding="utf-8") as f:
        json.dump(updated_blocks, f, indent=2)
    with open(structured_txt_path, "w", encoding="utf-8") as f:
        f.write(structured_text)

    try:
        html_content = generate_anonymized_html(final_masked_doc, global_combined_map)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)
    except Exception as e:
        logger.warning(f"⚠️ HTML generation failed: {e}")
        html_path = None

    try:
        raw_html = generate_anonymized_html(structured_text, {})
        with open(structured_html_path, "w", encoding="utf-8") as f:
            f.write(raw_html)
    except Exception as e:
        logger.warning(f"⚠️ Structured HTML generation failed: {e}")
        structured_html_path = None
    '''
    
    # these are fine once structured_text is guaranteed non-None
    with open(structured_json_path, "w", encoding="utf-8") as f:
        json.dump(updated_blocks, f, indent=2)
    with open(structured_txt_path, "w", encoding="utf-8") as f:
        f.write(structured_text)

    try:
        html_content = generate_anonymized_html(final_masked_doc, global_combined_map)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)
    except Exception as e:
        logger.warning(f"⚠️ HTML generation failed: {e}")
        html_path = None

    try:
        raw_html = generate_anonymized_html(structured_text, {})  # ✅ structured_text is a string now
        with open(structured_html_path, "w", encoding="utf-8") as f:
            f.write(raw_html)
    except Exception as e:
        logger.warning(f"⚠️ Structured HTML generation failed: {e}")
        structured_html_path = None


    # risk_result = service.compute_risk_score(final_masked_doc, global_presidio_map, global_spacy_map)


    # Get original text from File model (prefer DB content, else read file)
    '''
    original_for_risk = file_entry.content
    if not original_for_risk:
        try:
            with open(file_entry.filepath, "r", encoding="utf-8") as f:
                original_for_risk = f.read()
        except Exception:
            original_for_risk = final_masked_doc  # last resort fallback
    '''

    original_for_risk = file_entry.content
    if not original_for_risk:
        try:
            extracted_text, _, _ = service.extract_text_from_file(file_entry.filepath)
            original_for_risk = extracted_text or ""
        except Exception:
            original_for_risk = ""

    if not original_for_risk.strip():
        original_for_risk = final_masked_doc  # last resort


    risk_result = service.compute_risk_score(original_for_risk, global_presidio_map, global_spacy_map)

    if run_id:
        anonymization_run = get_object_or_404(AnonymizationRun, id=run_id)
        anonymization_run.status = "Completed"
        anonymization_run.save(update_fields=["status"])
        # run = file_entry.run 
    else:
        anonymization_run = AnonymizationRun.objects.create(
                project_id=file_entry.project_id,
                service_id=file_entry.service_id,
                client=getattr(file_entry.user, 'client', None),
                client_name=file_entry.user.username or file_entry.user.email,
                status="Completed",
                anonymization_type="Presidio-Spacy"
                )

        # run = file_entry.run # ✅ Still fallback to the File’s Run

    with transaction.atomic():
        
        existing = Anonymize.objects.filter(original_file=file_entry, file_type=file_type, is_active=True).first()
        if existing:
            return {
                    "status": "Already Anonymized",
                    "anonymized_id": str(existing.id),
                    "file_id": file_id,
                    "message": "An active anonymization already exists for this file."
                    }

        Anonymize.objects.filter(original_file=file_entry, file_type=file_type, is_active=True).update(is_active=False)

        Anonymize.objects.create(
            original_file=file_entry,
            run=anonymization_run,
            file_type=file_type,
            is_active=True,
            anonymized_filepath=txt_path,
            anonymized_html_filepath=html_path,
            entity_mapping_filepath=json_path,
            anonymized_structured_filepath=structured_json_path if file_type == "structured" else None,
            anonymized_structured_txt_filepath=structured_txt_path if file_type == "structured" else None,
            anonymized_structured_html_filepath=structured_html_path if file_type == "structured" else None,
            anonymized_markdown_filepath=None,
            presidio_masking_map=global_presidio_map,
            spacy_masking_map=global_spacy_map,
            risk_score=risk_result["risk_score"],
            risk_level=risk_result["risk_level"],
            risk_breakdown=risk_result["breakdown"],
            status="Completed"
        )


        # ✅ Register each generated file in the File table
        user = file_entry.user
        project_id = file_entry.project_id
        service_id = file_entry.service_id

        registered_files = []

        upload_run = file_entry.run  # This is from core.models.Run

        for path in [
            txt_path,
            json_path,
            html_path,
            structured_json_path,
            structured_txt_path,
            structured_html_path
        ]:
            if path and os.path.exists(path):
                registered = register_generated_file(
                    file_path=path,
                    user=user,
                    run=upload_run,
                    project_id=project_id,
                    service_id=service_id,
                    folder_name="anonymized"
                )
                registered_files.append({
                    "filename": registered.filename,
                    "file_id": registered.id,
                    "path": registered.filepath
                })

    return {
        "file_id": file_id,
        #"anonymization_run_id": str(run.id),
        "anonymization_run_id": str(anonymization_run.id),
        "file_type": file_type,
        "status": "Completed",
        "registered_outputs": registered_files
    }


@shared_task
def deanonymize_document_task(file_id):
    logger.info(f"🔄 Starting de-anonymization for file_id={file_id}")

    instance = get_object_or_404(Anonymize, original_file_id=file_id, is_active=True, file_type="plain")

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
        DeAnonymize.objects.create(
            file=instance.original_file,
            unmasked_text=final_text,
            unmasked_filepath=txt_path,
            presidio_masking_map=instance.presidio_masking_map,
            spacy_masking_map=instance.spacy_masking_map,
            status="Completed"
        )

    logger.info(f"✅ De-anonymization complete for file_id={file_id}")
    return {"file_id": file_id, "deanonymized_txt": txt_path, "status": "Completed"}


'''
@shared_task
def compute_risk_score_task(file_id):
    logger.info(f"📊 Computing risk score for file_id={file_id}")
    file_entry = get_object_or_404(File, id=file_id)
    # instance = get_object_or_404(Anonymize, original_file=file_entry, is_active=True)
    instance = Anonymize.objects.filter(original_file=file_entry, is_active=True).first()
    if not instance:
        return {"error": "No active anonymized file found."}

    if not instance.anonymized_filepath or not os.path.exists(instance.anonymized_filepath):
        return {"error": "Masked file not found"}

    # with open(instance.anonymized_filepath, "r", encoding="utf-8") as f:
    #     masked_text = f.read()

    # presidio_map = instance.presidio_masking_map or {}
    # spacy_map = instance.spacy_masking_map or {}

    # risk_result = service.compute_risk_score(masked_text, presidio_map, spacy_map)

    presidio_map = instance.presidio_masking_map or {}
    spacy_map = instance.spacy_masking_map or {}

    # Prefer original content from File, fallback to reading from disk
    original_for_risk = file_entry.content
    if not original_for_risk:
        try:
            with open(file_entry.filepath, "r", encoding="utf-8") as f:
                original_for_risk = f.read()
        except Exception:
            original_for_risk = None

    if not original_for_risk:
        return {"error": "No source text available for risk scoring"}

    risk_result = service.compute_risk_score(original_for_risk, presidio_map, spacy_map)

    instance.risk_score = risk_result["risk_score"]
    instance.risk_level = risk_result["risk_level"]
    instance.risk_breakdown = risk_result["breakdown"]
    instance.save(update_fields=["risk_score", "risk_level", "risk_breakdown", "updated_at"])

    return {
        "file_id": file_id,
        "risk_score": risk_result["risk_score"],
        "risk_level": risk_result["risk_level"],
        "breakdown": risk_result["breakdown"]
    }
'''

@shared_task
def compute_risk_score_task(file_id):
    logger.info(f"📊 Computing risk score for file_id={file_id}")
    file_entry = get_object_or_404(File, id=file_id)
    instance = Anonymize.objects.filter(original_file=file_entry, is_active=True).first()
    if not instance:
        return {"error": "No active anonymized file found."}

    presidio_map = instance.presidio_masking_map or {}
    spacy_map    = instance.spacy_masking_map or {}

    # Prefer DB text; else extract with service; else fail over to masked
    original_for_risk = file_entry.content
    if not original_for_risk:
        try:
            extracted_text, _, _ = service.extract_text_from_file(file_entry.filepath)
            original_for_risk = extracted_text or ""
        except Exception:
            original_for_risk = ""

    if not original_for_risk.strip():
        # still nothing; fallback to masked text on disk if available
        if instance.anonymized_filepath and os.path.exists(instance.anonymized_filepath):
            try:
                with open(instance.anonymized_filepath, "r", encoding="utf-8") as f:
                    original_for_risk = f.read()
            except Exception:
                original_for_risk = ""

    if not original_for_risk.strip():
        return {"error": "No source text available for risk scoring"}

    risk_result = service.compute_risk_score(original_for_risk, presidio_map, spacy_map)

    instance.risk_score     = risk_result["risk_score"]
    instance.risk_level     = risk_result["risk_level"]
    instance.risk_breakdown = risk_result["breakdown"]
    instance.save(update_fields=["risk_score", "risk_level", "risk_breakdown", "updated_at"])

    return {
        "file_id": file_id,
        "risk_score": risk_result["risk_score"],
        "risk_level": risk_result["risk_level"],
        "breakdown": risk_result["breakdown"],
    }


@shared_task
def compute_anonymization_stats_task(
    client_name=None,
    project_id=None,
    service_id=None,
    date_from=None,
    date_to=None
):
    logger.info(f"📊 Computing anonymization stats...")

    result = compute_global_anonymization_stats(
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

    logger.info(f"✅ Stats saved to DB (id={stats_record.id})")

    return {
        "stats_id": str(stats_record.id),
        **result,
    }




@shared_task
def generate_anonymization_insights_task(user_id):
    """
    Celery task to compute anonymization insights for a single user.
    """
    logger.info(f"📊 Generating anonymization insights for user_id={user_id}")

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.error(f"❌ User not found: {user_id}")
        return {"error": f"User {user_id} not found"}

    insights = calculate_anonymization_insights(user)

    logger.info(f"✅ Anonymization insights generated for user_id={user_id}")
    return insights

