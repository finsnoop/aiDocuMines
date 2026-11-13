from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from django.shortcuts import get_object_or_404
from oauth2_provider.models import Application
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi

from core.models import File
from document_anonymizer.models import AnonymizationRun, Anonymize, DeAnonymize

import logging
import os
import json
from core.access import get_file_for_user, get_scoped_or_404, get_run_for_user, scope_by_file, scope_by_owner

logger = logging.getLogger(__name__)

# Swagger Parameters
client_id_param = openapi.Parameter("X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True)
client_secret_param = openapi.Parameter("X-Client-Secret", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True)
file_id_param = openapi.Parameter("file_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False)
file_type_param = openapi.Parameter("file_type", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False)
run_id_param = openapi.Parameter("run_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False)

def get_user_from_client_id(client_id):
    try:
        application = Application.objects.get(client_id=client_id)
        return application.user
    except Application.DoesNotExist:
        return None




def normalize_file_type(ft):
    """Normalize file_type aliases."""
    if not ft:
        return None
    ft = ft.strip().lower()
    if ft in {"txt", "text"}:
        return "plain"
    if ft in {"plain", "structured"}:
        return ft
    # Fallback: return as-is (but lowercased)
    return ft




class AnonymizationTaskStatusView(APIView):
    """
    Retrieve the status of an anonymization or de-anonymization process using `file_id` or `run_id`.
    Handles in-flight and failed runs gracefully.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Check anonymization/de-anonymization status using file_id or run_id.",
        tags=["Anonymization Status"],
        manual_parameters=[client_id_param, client_secret_param, file_id_param, file_type_param, run_id_param],
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        file_id = request.query_params.get("file_id")
        file_type = normalize_file_type(request.query_params.get("file_type"))
        run_id = request.query_params.get("run_id")

        if not client_id or not client_secret or (not file_id and not run_id):
            return Response({"error": "Missing required parameters"}, status=400)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=403)

        run_instance = None
        anonymized_file = None

        # Prefer run_id if provided
        if run_id:
            run_instance = get_run_for_user(request.user, AnonymizationRun, run_id)

        # If no run_id, try to resolve via file_id + file_type
        elif file_id:
            if not file_type:
                return Response({"error": "file_type must be provided when using file_id"}, status=400)

            file_instance = get_file_for_user(request.user, file_id)
            try:
                anonymized_file = Anonymize.objects.get(
                    original_file=file_instance, file_type=file_type, is_active=True
                )
            except Anonymize.DoesNotExist:
                # Could still be processing: try to surface run status if present
                # (Find the latest run tied to this file via any anonymize entry)
                possible = scope_by_file(request.user, Anonymize.objects.filter(original_file=file_instance), 'original_file').order_by("-created_at").first()
                if possible and possible.run:
                    run_instance = possible.run
                else:
                    return Response({"error": "No active anonymized file found for given file_id and file_type"}, status=404)
            except Anonymize.MultipleObjectsReturned:
                return Response({"error": "Multiple active anonymizations found. Check data integrity."}, status=500)

            if anonymized_file and anonymized_file.run:
                run_instance = anonymized_file.run

        # If still no run_instance, bail
        if not run_instance:
            return Response({"error": "No anonymization run found for the provided input"}, status=404)

        # If we don't yet have an artifact row, try to find one under the run
        if not anonymized_file:
            q = Anonymize.objects.filter(run=run_instance).order_by("-created_at")
            if file_type:
                q = q.filter(file_type=file_type)
            anonymized_file = q.first()

        # If no artifact row was found, respond based on run status
        if not anonymized_file:
            status_val = run_instance.status
            error_msg = getattr(run_instance, "error_message", None)

            if status_val in {"Queued", "Processing"}:
                return Response({
                    "anonymization_run_id": str(run_instance.id),
                    "original_file_id": None,
                    "anonymized_file_id": None,
                    "status": status_val,
                    "anonymization_type": getattr(run_instance, "anonymization_type", None),
                    "error_message": error_msg,
                    "project_id": getattr(run_instance, "project_id", None),
                    "service_id": getattr(run_instance, "service_id", None),
                    "client_name": getattr(run_instance, "client_name", None),
                    "note": "Artifacts not ready yet; poll again."
                }, status=200)

            if status_val == "Failed":
                return Response({
                    "anonymization_run_id": str(run_instance.id),
                    "original_file_id": None,
                    "anonymized_file_id": None,
                    "status": "Failed",
                    "anonymization_type": getattr(run_instance, "anonymization_type", None),
                    "error_message": error_msg or "Anonymization failed.",
                    "project_id": getattr(run_instance, "project_id", None),
                    "service_id": getattr(run_instance, "service_id", None),
                    "client_name": getattr(run_instance, "client_name", None)
                }, status=200)

            # Completed but no artifact recorded -> data mismatch
            return Response({"error": "Run is Completed but no artifact was found."}, status=500)

        # At this point we have an artifact row
        de_anonymized_file = scope_by_file(request.user, DeAnonymize.objects.filter(file=anonymized_file.original_file), 'file').first()
        entity_mapping = anonymized_file.spacy_masking_map or anonymized_file.presidio_masking_map or {}

        # Limit registered outputs to anonymized folder only (avoid mixing unrelated run files)
        registered_outputs = list(
            File.objects.filter(run=anonymized_file.original_file.run)
            .exclude(id=anonymized_file.original_file.id)
            .filter(filepath__icontains="/anonymized/")
            .values("id", "filename", "filepath")
        )

        return Response({
            "anonymization_run_id": str(run_instance.id),
            "original_file_id": str(anonymized_file.original_file.id),
            "anonymized_file_id": str(anonymized_file.id),
            "status": run_instance.status,
            "anonymization_type": getattr(run_instance, "anonymization_type", None),
            "error_message": getattr(run_instance, "error_message", None) if run_instance.status == "Failed" else None,
            "project_id": anonymized_file.original_file.project_id,
            "service_id": anonymized_file.original_file.service_id,
            "client_name": getattr(run_instance, "client_name", None),
            "original_file_path": anonymized_file.original_file.filepath,
            "anonymized_file_path": anonymized_file.anonymized_filepath,
            "anonymized_html_path": anonymized_file.anonymized_html_filepath,
            "anonymized_markdown_path": anonymized_file.anonymized_markdown_filepath,
            "anonymized_structured_path": anonymized_file.anonymized_structured_filepath,
            "deanonymized_file_path": de_anonymized_file.unmasked_filepath if de_anonymized_file else "N/A",
            "created_at": anonymized_file.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": anonymized_file.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
            "entity_mapping": entity_mapping,
            "outputs": {
                "txt": anonymized_file.anonymized_filepath,
                "html": anonymized_file.anonymized_html_filepath,
                "markdown": anonymized_file.anonymized_markdown_filepath,
                "structured": anonymized_file.anonymized_structured_filepath,
                "deanonymized": de_anonymized_file.unmasked_filepath if de_anonymized_file else None
            },
            "registered_outputs": registered_outputs
        }, status=200)


