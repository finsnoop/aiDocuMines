import os
import logging
import json
from celery import shared_task
from django.db import transaction
from django.shortcuts import get_object_or_404
from core.models import File
from core.utils import register_generated_file
from document_automation.models import AutomationRun, AutomationTemplate, AutomationResult, TemplateField
from document_automation.utils import process_template, generate_output_document, extract_template_fields, merge_bulk_outputs, inject_clause_into_docx

logger = logging.getLogger(__name__)


@shared_task
def process_automation_task(run_id):
    logger.info(f"Starting automation for run_id={run_id}")

    automation_run = get_object_or_404(AutomationRun, id=run_id)
    template = automation_run.template

    automation_run.status = "Processing"
    automation_run.save()

    try:
        if template.file_id is None:
            raise FileNotFoundError("This template has no source document. Reattach its source before running it.")
        if not os.path.exists(template.file.filepath):
            raise FileNotFoundError(f"Template file not found: {template.file.filepath}")

        _extract_and_store_fields(template)

        bulk_count = automation_run.bulk_count or 1
        output_paths = []
        pdf_paths = []

        if bulk_count > 1 and automation_run.bulk_data:
            data_items = automation_run.bulk_data
        elif bulk_count > 1 and not automation_run.bulk_data:
            data_items = [automation_run.input_data or {} for _ in range(bulk_count)]
        else:
            data_items = [automation_run.input_data or {}]

        for idx, input_data in enumerate(data_items):
            rendered_content = process_template(
                template.file.filepath,
                template.template_type,
                input_data,
            )

            ext_map = {'DOCX': '.docx', 'TXT': '.txt', 'HTML': '.html'}
            ext = ext_map.get(template.template_type, '.txt')

            if bulk_count > 1:
                output_filename = f"{os.path.splitext(template.file.filename)[0]}_output_{idx+1}{ext}"
            else:
                output_filename = f"{os.path.splitext(template.file.filename)[0]}_output{ext}"

            output_dir = os.path.join(os.path.dirname(template.file.filepath), "automation_output", str(automation_run.id))
            output_path = os.path.join(output_dir, output_filename)

            output_path, pdf_output_path = generate_output_document(
                rendered_content,
                template.template_type,
                output_path,
                output_format=automation_run.output_format,
            )

            if pdf_output_path:
                pdf_paths.append(pdf_output_path)
            output_paths.append(output_path)

            if automation_run.clause_ids:
                clause_models = _get_clauses(automation_run.clause_ids)
                for clause in clause_models:
                    inject_clause_into_docx(output_path, clause, 0)

            with transaction.atomic():
                variables_used = list(input_data.keys()) if input_data else None
                result = AutomationResult.objects.create(
                    run=automation_run,
                    output_filepath=output_path,
                    pdf_output_path=pdf_output_path,
                    output_filename=output_filename,
                    variables_used=variables_used,
                    generation_index=idx,
                    status="Completed",
                )

                if template.file.user:
                    registered = register_generated_file(
                        file_path=output_path,
                        user=template.file.user,
                        run=automation_run,
                        project_id=automation_run.project_id,
                        service_id=automation_run.service_id,
                        folder_name=os.path.join("automation", str(automation_run.id)),
                    )

                result.output_filepath = output_path
                result.save()

        if len(output_paths) > 1:
            merged_filename = f"{os.path.splitext(template.file.filename)[0]}_merged{ext}"
            merged_path = os.path.join(
                os.path.dirname(template.file.filepath), "automation_output", str(automation_run.id), merged_filename
            )
            merge_bulk_outputs(output_paths, merged_path)

            if template.file.user:
                register_generated_file(
                    file_path=merged_path,
                    user=template.file.user,
                    run=automation_run,
                    project_id=automation_run.project_id,
                    service_id=automation_run.service_id,
                    folder_name=os.path.join("automation", str(automation_run.id)),
                )

        automation_run.status = "Completed"
        automation_run.save()

        logger.info(f"Automation completed for run_id={run_id}")
        return {
            "run_id": str(automation_run.id),
            "status": "Completed",
            "output_count": len(output_paths),
            "output_paths": output_paths,
            "pdf_paths": pdf_paths,
        }

    except Exception as e:
        logger.error(f"Automation failed for run_id={run_id}: {e}")
        automation_run.status = "Failed"
        automation_run.error_message = str(e)
        automation_run.save()

        AutomationResult.objects.create(
            run=automation_run,
            output_filepath=None,
            status="Failed",
        )

        return {"error": str(e), "run_id": run_id}


def _extract_and_store_fields(template):
    if template.file_id is None or not os.path.exists(template.file.filepath):
        return

    field_names = extract_template_fields(template.file.filepath, template.template_type)
    if not field_names:
        return

    existing = set(TemplateField.objects.filter(template=template).values_list('name', flat=True))
    new_fields = []
    for order, name in enumerate(field_names):
        if name not in existing:
            new_fields.append(TemplateField(
                template=template,
                name=name,
                field_type='text',
                required=False,
                order=order,
            ))
    if new_fields:
        TemplateField.objects.bulk_create(new_fields, ignore_conflicts=True)


def _get_clauses(clause_ids):
    from document_automation.models import Clause

    if not clause_ids:
        return []
    return list(Clause.objects.filter(id__in=clause_ids, is_active=True))
