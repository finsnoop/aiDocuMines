# app_layout/tasks.py

from celery import shared_task
from django.db import transaction
from django.conf import settings
from core.models import File
from app_layout.models import CatalogEntry, TaskStatus, ClientPersona, SuggestedSolution, ProposalDraft, IncomingRFP


from app_layout.utils import (
    extract_text_from_pdf,
    generate_page_hash,
    build_product_service_prompt,
    build_client_persona_prompt,
    build_product_recommendation_prompt,
    build_proposal_prompt,
    build_suggested_solutions_prompt,   # ADD THIS
    call_gpt4o,
    save_proposal_to_docx,
    build_rfp_extraction_prompt,
    build_rfp_chat_prompt,
    extract_text_from_pdf,
    try_parse_json_from_response,
    extract_rfp_metadata

)

import os
import json

@shared_task(bind=True)
def process_document_task(self, document_id, run_id=None):
    """Process an uploaded document and extract catalog entries."""
    try:
        document = File.objects.get(id=document_id)
        file_path = document.filepath

        # Create task status entry
        TaskStatus.objects.create(
            document=document,
            task_type='catalog_extraction',
            status='processing',
            message=f"Run ID: {run_id}" if run_id else "No Run ID provided"
        )

        pages = extract_text_from_pdf(file_path)
        existing_hashes = set(CatalogEntry.objects.filter(document=document).values_list('page_hash', flat=True))

        with transaction.atomic():
            for page in pages:
                page_hash = generate_page_hash(page)
                if page_hash in existing_hashes:
                    continue  # Skip already processed pages

                prompt = build_product_service_prompt(page)
                result = call_gpt4o(prompt)

                if result and result.get("name"):
                    CatalogEntry.objects.create(
                        document=document,
                        name=result.get("name", ""),
                        details=result.get("details", ""),
                        solves=result.get("solves", ""),
                        target_clients=result.get("target_clients", {}),
                        page_hash=page_hash,
                    )

        TaskStatus.objects.filter(document=document, task_type='catalog_extraction').update(
            status='completed',
            message=f'Document processed successfully. Run ID: {run_id}' if run_id else 'Document processed successfully.'
        )

    except Exception as e:
        TaskStatus.objects.filter(document_id=document_id, task_type='catalog_extraction').update(
            status='failed',
            message=str(e)
        )
        raise self.retry(exc=e, countdown=30, max_retries=3)


@shared_task(bind=True)
def generate_client_persona_task(self, company_name, user_id):
    """Generate and save a standardized client persona."""
    try:
        from app_layout.models import User
        user = User.objects.get(id=user_id)

        prompt = build_client_persona_prompt(company_name)
        result = call_gpt4o(prompt)

        if result and isinstance(result, dict):
            ClientPersona.objects.create(
                user=user,
                search_query=company_name,
                standardized_persona=result
            )

    except Exception as e:
        raise self.retry(exc=e, countdown=30, max_retries=3)



@shared_task(bind=True)
def generate_proposal_draft_task(self, client_persona_id, additional_info="", focus_solution=None, context_solutions=None):
    """Generate a proposal draft, possibly focused on a specific solution, and save it to the database and DOCX."""
    try:
        persona = ClientPersona.objects.get(id=client_persona_id)
        persona_data = persona.standardized_persona

        # Updated prompt builder with optional solution/context
        prompt = build_proposal_prompt(
            persona_json=persona_data,
            additional_info=additional_info,
            focus_solution=focus_solution,
            context_solutions=context_solutions
        )

        result = call_gpt4o(prompt)

        if result:
            if isinstance(result, dict):
                content = result.get("proposal_draft") or result.get("answer", "Draft could not be generated.")
            else:
                content = result

            # Save to DB
            proposal = ProposalDraft.objects.create(
                persona=persona,
                content=content,
                additional_notes=additional_info
            )

            # Generate and store DOCX
            save_proposal_to_docx(proposal)

    except Exception as e:
        raise self.retry(exc=e, countdown=30, max_retries=3)


@shared_task(bind=True)
def match_persona_to_catalog_task(self, persona_id):
    """Match a client persona to the full catalog (across all documents) and recommend solutions."""
    from app_layout.models import ClientPersona, CatalogEntry, SuggestedSolution

    try:
        persona = ClientPersona.objects.get(id=persona_id)
        catalog_entries = CatalogEntry.objects.all()

        persona_json = persona.standardized_persona
        catalog_json = [
            {
                "name": entry.name,
                "details": entry.details,
                "solves": entry.solves,
                "target_clients": entry.target_clients,
            }
            for entry in catalog_entries
        ]

        from app_layout.utils import build_suggested_solutions_prompt, call_gpt4o

        prompt = build_suggested_solutions_prompt(json.dumps(persona_json), json.dumps(catalog_json))
        result = call_gpt4o(prompt)

        if result and isinstance(result, list):
            with transaction.atomic():
                # Clear old suggestions for this persona
                SuggestedSolution.objects.filter(persona=persona).delete()

                # Save new suggestions
                for suggestion in result:
                    SuggestedSolution.objects.create(
                        persona=persona,
                        product_or_service=suggestion.get("product_or_service", ""),
                        reason_for_relevance=suggestion.get("reason_for_relevance", ""),
                    )
    except Exception as e:
        raise self.retry(exc=e, countdown=30, max_retries=3)


@shared_task(bind=True)
def generate_suggested_solutions_task(self, persona_id):
    """Generate suggested solutions and save them."""
    try:
        persona = ClientPersona.objects.get(id=persona_id)
        persona_data = persona.standardized_persona

        # Build catalog from all Morae entries
        catalog_entries = CatalogEntry.objects.all()
        catalog_data = [
            {
                "name": entry.name,
                "details": entry.details,
                "solves": entry.solves,
                "target_clients": entry.target_clients,
            }
            for entry in catalog_entries
        ]

        # Build prompt and call GPT-4o
        prompt = build_suggested_solutions_prompt(persona_data, catalog_data)
        result = call_gpt4o(prompt)

        # Save results
        if isinstance(result, dict) and "recommendations" in result:
            recommendations = result["recommendations"]

            with transaction.atomic():
                for rec in recommendations:
                    SuggestedSolution.objects.create(
                        persona=persona,
                        product_or_service=rec.get("product_or_service", ""),
                        reason_for_relevance=rec.get("reason_for_relevance", rec.get("reason", "")),
                    )
        elif isinstance(result, list):  # In case GPT returns a raw list
            with transaction.atomic():
                for rec in result:
                    SuggestedSolution.objects.create(
                        persona=persona,
                        product_or_service=rec.get("product_or_service", ""),
                        reason_for_relevance=rec.get("reason_for_relevance", rec.get("reason", "")),
                    )
        else:
            print("No proper recommendations found in GPT response:", result)

    except Exception as e:
        raise self.retry(exc=e, countdown=30, max_retries=3)


@shared_task(bind=True)
def extract_rfp_sections_task(self, rfp_id):
    """Extract sections from an uploaded RFP file using GPT-4o."""
    try:
        rfp = IncomingRFP.objects.get(id=rfp_id)
        document = rfp.file
        
        if not document:
            raise ValueError("Document not found for the provided RFP ID.")
        if not document.filepath:
            raise ValueError("Document filepath is empty or invalid.")
        
        file_path = os.path.join(settings.MEDIA_ROOT, document.filepath)
        
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File does not exist: {file_path}")

        # Extract full text from the PDF
        all_text = "\n".join(extract_text_from_pdf(file_path))

        prompt = build_rfp_extraction_prompt(all_text)
        result = call_gpt4o(prompt)

        if isinstance(result, dict):
            summary = result.get("summary", "")
            sections = result.get("parsed_sections", {})

            rfp.summary = summary
            rfp.parsed_sections = sections
            rfp.status = "completed"
            rfp.save()

        else:
            rfp.status = "failed"
            rfp.save()
            print("GPT response for RFP was not a valid dictionary")

    except Exception as e:
        print(f"RFP parsing failed: {e}")
        if rfp_id:
            IncomingRFP.objects.filter(id=rfp_id).update(status="failed")
        raise self.retry(exc=e, countdown=30, max_retries=3)


@shared_task(bind=True)
def answer_rfp_chat_task(self, rfp_id, question, history=""):
    """Generate a GPT-based response to a chat question about an RFP."""
    try:
        rfp = IncomingRFP.objects.get(id=rfp_id)
        summary = {
            "summary": rfp.summary,
            "parsed_sections": rfp.parsed_sections,
        }
        prompt = build_rfp_chat_prompt(summary, question, history)
        return call_gpt4o(prompt)
    except Exception as e:
        print(f"GPT chat failed: {e}")
        return "GPT failed to respond."


@shared_task(bind=True)
def extract_rfp_metadata_task(self, rfp_id):
    """
    Extract metadata like client name, region, contact, and due date from RFP text using GPT.
    """
    try:
        rfp = IncomingRFP.objects.get(id=rfp_id)
        file_path = os.path.join(settings.MEDIA_ROOT, rfp.file.filepath)
        
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File does not exist: {file_path}")

        full_text = "\n".join(extract_text_from_pdf(file_path))
        metadata = extract_rfp_metadata(full_text)

        if metadata:
            rfp.client_name = metadata.get("client_name", "")
            rfp.rfp_title = metadata.get("rfp_title", "")
            rfp.due_date = metadata.get("due_date") or None
            rfp.region = metadata.get("region", "")
            rfp.contact_name = metadata.get("contact_name", "")
            rfp.contact_email = metadata.get("contact_email", "")
            rfp.save()
        else:
            print("No metadata extracted from RFP.")

    except Exception as e:
        print(f"Metadata extraction failed: {e}")
        raise self.retry(exc=e, countdown=30, max_retries=3)

