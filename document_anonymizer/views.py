import os
import json
import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from django.contrib.auth import get_user_model
from django.http import FileResponse
from oauth2_provider.models import Application
from oauth2_provider.contrib.rest_framework import OAuth2Authentication, TokenHasReadWriteScope
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from core.models import File
from core.tenancy import ClientScopedViewMixin
from custom_authentication.permissions import IsAdminOrManagerMutation
from document_anonymizer.models import Anonymize, DeAnonymize, AnonymizationRun
from document_anonymizer.tasks import anonymize_document_task, deanonymize_document_task
from document_anonymizer.llm_orchestrator import query_model  # New import

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import JSONParser
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from django.shortcuts import get_object_or_404
from document_anonymizer.models import Anonymize
from core.models import File
from document_anonymizer.utils import export_to_markdown, export_to_docx, summarize_blocks, update_structured_block
from custom_authentication.permissions import IsClientOrAdminOrSuperUser
from document_anonymizer.tasks import compute_risk_score_task
import uuid
from document_anonymizer.tasks import compute_anonymization_stats_task
from celery.result import AsyncResult
from rest_framework.pagination import PageNumberPagination
from rest_framework import generics
from document_anonymizer.models import AnonymizationStats
from document_anonymizer.serializers import AnonymizationStatsSerializer
from document_anonymizer.pagination import StandardResultsSetPagination
from document_anonymizer.tasks import generate_anonymization_insights_task

import json
import os

from django.core.cache import cache
from datetime import datetime

logger = logging.getLogger(__name__)
User = get_user_model()

# Swagger Parameters
client_id_param = openapi.Parameter("X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True)
client_secret_param = openapi.Parameter("X-Client-Secret", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True)
file_id_param = openapi.Parameter("file_id", openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True)
project_id_param = openapi.Parameter("project_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True)
service_id_param = openapi.Parameter("service_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True)
file_type_param = openapi.Parameter("file_type", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description="plain or structured")
file_type_param = openapi.Parameter("file_type", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description="plain or structured")
variant_param = openapi.Parameter("variant", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description="Either 'original' or 'anonymized'")



def health_check(request):
    from django.http import JsonResponse
    return JsonResponse({"status": "ok"}, status=200)

def get_user_from_client_id(client_id):
    try:
        application = Application.objects.get(client_id=client_id)
        return application.user
    except Application.DoesNotExist:
        return None




class SubmitAnonymizationAPIView(ClientScopedViewMixin):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        manual_parameters=[
            client_id_param,
            client_secret_param,
            file_id_param,
            project_id_param,
            service_id_param,
            file_type_param
        ],
        operation_description="Starts anonymization using the default Presidio ➝ spaCy pipeline. You can optionally specify the file_type (plain or structured)."
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        file_id = request.query_params.get("file_id")
        project_id = request.query_params.get("project_id")
        service_id = request.query_params.get("service_id")
        file_type = request.query_params.get("file_type", "plain").lower()

        if not all([client_id, client_secret, file_id, project_id, service_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        application = get_object_or_404(Application, client_id=client_id)
        user = application.user
        file_instance = self.get_object_by_client(File.objects.filter(project_id=project_id, service_id=service_id), id=file_id)

        if str(file_instance.user.id) != str(user.id):
            raise PermissionDenied("You are not authorized to process this file.")

        if not os.path.exists(file_instance.filepath):
            return Response({"error": "File not found."}, status=status.HTTP_404_NOT_FOUND)

        # ✅ CHECK FOR EXISTING COMPLETED ANONYMIZATION
        existing = Anonymize.objects.filter(
            original_file=file_instance,
            file_type=file_type,
            status="Completed",
            is_active=True
        ).first()

        if existing:
            return Response({
                "message": "This file has already been anonymized.",
                "anonymized_id": str(existing.id),
                "anonymized_filepath": existing.anonymized_filepath,
                "anonymized_structured_filepath": existing.anonymized_structured_filepath,
                "status": "Completed"
            }, status=status.HTTP_200_OK)

        # Proceed with new anonymization with client FK
        anonymization_run = AnonymizationRun.objects.create(
            id=str(uuid.uuid4()),
            project_id=project_id,
            service_id=service_id,
            client=request.user.client,
            client_name=user.username or user.email,
            status="Processing",
            anonymization_type="Presidio-Spacy"
        )

        anonymize_document_task.delay(file_id, file_type, str(anonymization_run.id))

        return Response({
            "anonymization_run_id": str(anonymization_run.id),
            "file_id": str(file_instance.id),
            "status": "Processing"
        }, status=status.HTTP_202_ACCEPTED)




'''
class SubmitAnonymizationAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        manual_parameters=[
            client_id_param,
            client_secret_param,
            file_id_param,
            project_id_param,
            service_id_param,
            file_type_param
        ],
        operation_description="Starts anonymization using the default Presidio ➝ spaCy pipeline. You can optionally specify the file_type (plain or structured)."
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        file_id = request.query_params.get("file_id")
        project_id = request.query_params.get("project_id")
        service_id = request.query_params.get("service_id")
        file_type = request.query_params.get("file_type", "plain").lower()

        if not all([client_id, client_secret, file_id, project_id, service_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        application = get_object_or_404(Application, client_id=client_id)
        user = application.user
        file_instance = get_object_or_404(File, id=file_id, project_id=project_id, service_id=service_id)

        if str(file_instance.user.id) != str(user.id):
            raise PermissionDenied("You are not authorized to process this file.")

        if not os.path.exists(file_instance.filepath):
            return Response({"error": "File not found."}, status=status.HTTP_404_NOT_FOUND)


        anonymization_run = AnonymizationRun.objects.create(
                id=str(uuid.uuid4()),  # force UUID for consistency
                project_id=project_id,
                service_id=service_id,
                client=getattr(user, 'client', None),
                client_name=user.username or user.email,
                status="Processing",
                anonymization_type="Presidio-Spacy"
        )

        anonymize_document_task.delay(file_id, file_type, str(anonymization_run.id))

        # anonymization_run = AnonymizationRun.objects.create(
        #     project_id=project_id,
        #     service_id=service_id,
        #     client_name=user.username or user.email,
        #     status="Processing",
        #     anonymization_type="Presidio-Spacy"
        # )

        # anonymize_document_task.delay(file_id, file_type)

        return Response({
            "anonymization_run_id": str(anonymization_run.id),
            "file_id": str(file_instance.id),
            "status": "Processing"
        }, status=status.HTTP_202_ACCEPTED)
        '''





class DownloadAnonymizedFileAPIView(ClientScopedViewMixin):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        manual_parameters=[
            client_id_param,
            client_secret_param,
            file_id_param,
            openapi.Parameter("file_type", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True),
            openapi.Parameter("structured_blocks", openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False)
        ]
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        file_id = request.query_params.get("file_id")
        file_type = request.query_params.get("file_type")
        structured_blocks = request.query_params.get("structured_blocks", "false").lower() == "true"

        if not all([client_id, client_secret, file_id, file_type]):
            return Response({"error": "Missing required parameters"}, status=400)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=403)

        self.get_object_by_client(File.objects.all(), id=file_id)
        anonymized_file = get_scoped_or_404(request.user, Anonymize, 'original_file', original_file_id=file_id, file_type=file_type, is_active=True)
        de_anonymized_file = scope_by_file(request.user, DeAnonymize.objects.filter(file_id=file_id), 'file').first()

        if file_type == "structured":
            path = anonymized_file.anonymized_structured_filepath
            if not path or not os.path.exists(path):
                return Response({"error": "Structured file not found."}, status=404)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return Response(data, content_type="application/json") if structured_blocks else FileResponse(open(path, "rb"))

        if file_type == "json":
            if not os.path.exists(anonymized_file.entity_mapping_filepath):
                return Response({"error": "JSON file not found."}, status=404)
            with open(anonymized_file.entity_mapping_filepath, "r", encoding="utf-8") as f:
                return Response(json.load(f), content_type="application/json")

        if file_type == "html":
            if not os.path.exists(anonymized_file.anonymized_html_filepath):
                return Response({"error": "HTML file not found."}, status=404)
            return FileResponse(open(anonymized_file.anonymized_html_filepath, "rb"))

        file_path_map = {
            "original": anonymized_file.original_file.filepath,
            "anonymized": anonymized_file.anonymized_filepath,
            "deanonymized": de_anonymized_file.unmasked_filepath if de_anonymized_file else None
        }

        path = file_path_map.get(file_type)
        if not path or not os.path.exists(path):
            return Response({"error": f"Requested {file_type} file does not exist."}, status=404)
        return FileResponse(open(path, "rb"))

class SubmitDeAnonymizationAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(manual_parameters=[client_id_param, file_id_param])
    def post(self, request):
        file_id = request.query_params.get("file_id")
        anonymized_file = get_scoped_or_404(request.user, Anonymize, 'original_file', original_file_id=file_id)
        deanonymize_document_task.delay(file_id)
        return Response({
            "message": "De-anonymization started.",
            "file_id": str(file_id),
            "status": "Processing"
        }, status=status.HTTP_202_ACCEPTED)

class DownloadDeAnonymizedFileAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(manual_parameters=[client_id_param, file_id_param])
    def get(self, request):
        file_id = request.query_params.get("file_id")
        deanonymized = get_scoped_or_404(request.user, DeAnonymize, 'file', file_id=file_id)
        if not os.path.exists(deanonymized.unmasked_filepath):
            return Response({"error": "File not found."}, status=404)
        return FileResponse(open(deanonymized.unmasked_filepath, "rb"))


class StructuredBlocksView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(manual_parameters=[
        openapi.Parameter('file_id', openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True),
        openapi.Parameter('flat', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False),
        openapi.Parameter('include_coordinates', openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False),
    ])
    def get(self, request):
        file_id = request.query_params.get("file_id")
        flat = request.query_params.get("flat") == "true"
        include_coordinates = request.query_params.get("include_coordinates") == "true"

        instance = get_scoped_or_404(request.user, Anonymize, 'original_file', original_file_id=file_id, is_active=True, file_type="structured")
        path = instance.anonymized_structured_filepath

        with open(path, "r", encoding="utf-8") as f:
            blocks = json.load(f)

        if not include_coordinates:
            for block in blocks:
                block.pop("metadata", None)

        return Response(blocks)


class SummarizeBlocksView(APIView):
    parser_classes = [JSONParser]
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def post(self, request):
        file_id = request.data.get("file_id")
        element_ids = request.data.get("element_ids", [])
        model = request.data.get("model", "phi-2")  # Default to local
        api_key = request.data.get("api_key")  # Optional for GPT-style models

        instance = get_scoped_or_404(request.user, Anonymize, 'original_file', original_file_id=file_id, is_active=True, file_type="structured")
        path = instance.anonymized_structured_filepath

        with open(path, "r", encoding="utf-8") as f:
            blocks = json.load(f)

        summaries = query_model(blocks, element_ids, model=model, api_key=api_key)
        return Response({"summaries": summaries})


class EditBlockView(APIView):
    parser_classes = [JSONParser]
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def patch(self, request):
        file_id = request.data.get("file_id")
        element_id = request.data.get("element_id")
        new_text = request.data.get("new_text")

        instance = get_scoped_or_404(request.user, Anonymize, 'original_file', original_file_id=file_id, is_active=True, file_type="structured")
        path = instance.anonymized_structured_filepath

        updated = update_structured_block(path, element_id, new_text)
        return Response({"updated": updated})


class ExportBlocksView(APIView):
    parser_classes = [JSONParser]
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def post(self, request):
        file_id = request.data.get("file_id")
        export_format = request.data.get("export_format")

        instance = get_scoped_or_404(request.user, Anonymize, 'original_file', original_file_id=file_id, is_active=True, file_type="structured")
        path = instance.anonymized_structured_filepath

        if export_format == "markdown":
            export_path = export_to_markdown(path, generate_preview=True)
        elif export_format == "docx":
            export_path = export_to_docx(path)
        else:
            return Response({"error": "Invalid export format"}, status=400)

        return Response({"download": export_path})



class DownloadStructuredMarkdownView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        manual_parameters=[file_id_param, variant_param]
    )
    def get(self, request):
        file_id = request.query_params.get("file_id")
        variant = request.query_params.get("variant", "anonymized").lower()

        if variant == "original":
            file = get_file_for_user(request.user, file_id)
            path = file.filepath.replace(".pdf", ".md")
        else:
            instance = get_scoped_or_404(request.user, Anonymize, 'original_file', original_file_id=file_id, is_active=True, file_type="structured")
            path = instance.anonymized_structured_filepath.replace(".json", ".md")

        if not os.path.exists(path):
            return Response({"error": f"{variant.capitalize()} Markdown file not found."}, status=404)

        return FileResponse(open(path, "rb"), content_type="text/markdown")


class DownloadStructuredHTMLView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        manual_parameters=[file_id_param, variant_param]
    )
    def get(self, request):
        file_id = request.query_params.get("file_id")
        variant = request.query_params.get("variant", "anonymized").lower()

        if variant == "original":
            file = get_file_for_user(request.user, file_id)
            path = file.filepath.replace(".pdf", ".html")
        else:
            instance = get_scoped_or_404(request.user, Anonymize, 'original_file', original_file_id=file_id, is_active=True, file_type="structured")
            path = instance.anonymized_html_filepath

        if not os.path.exists(path):
            return Response({"error": f"{variant.capitalize()} structured HTML not found."}, status=404)

        return FileResponse(open(path, "rb"), content_type="text/html")


class DownloadStructuredTextView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        manual_parameters=[file_id_param, variant_param]
    )
    def get(self, request):
        file_id = request.query_params.get("file_id")
        variant = request.query_params.get("variant", "anonymized").lower()

        if variant == "original":
            file = get_file_for_user(request.user, file_id)
            if (file.file_type or "").startswith("text/"):
                path = file.filepath
            else:
                return Response({"error": "Original structured text is not available for this file type."}, status=400)
        else:
            instance = get_scoped_or_404(request.user, Anonymize, 'original_file', original_file_id=file_id, is_active=True, file_type="structured")
            path = instance.anonymized_filepath

        if not os.path.exists(path):
            return Response({"error": f"Structured {variant} text file not found."}, status=404)

        return FileResponse(open(path, "rb"), content_type="text/plain")


class DownloadStructuredJSONView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        manual_parameters=[file_id_param, variant_param]
    )
    def get(self, request):
        file_id = request.query_params.get("file_id")
        variant = request.query_params.get("variant", "anonymized").lower()

        if variant == "original":
            file = get_file_for_user(request.user, file_id)
            path = file.filepath.replace(".pdf", ".json")
        else:
            instance = get_scoped_or_404(request.user, Anonymize, 'original_file', original_file_id=file_id, is_active=True, file_type="structured")
            path = instance.anonymized_structured_filepath

        if not os.path.exists(path):
            return Response({"error": f"{variant.capitalize()} structured JSON not found."}, status=404)

        with open(path, "r", encoding="utf-8") as f:
            return Response(json.load(f), content_type="application/json")


from document_anonymizer.tasks import compute_risk_score_task
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from celery.result import AsyncResult

class DocumentRiskScoreView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(manual_parameters=[file_id_param])
    def get(self, request):
        file_id = request.query_params.get("file_id")
        if not file_id:
            return Response({"error": "file_id is required"}, status=400)

        # Check if risk score already exists in DB or cache in future
        task = compute_risk_score_task.delay(file_id)
        return Response({"task_id": task.id, "status": "Risk analysis started"}, status=202)


class DocumentRiskScoreResultView(APIView):
    """
    Fetches the result of a risk score analysis task
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(manual_parameters=[
        openapi.Parameter("task_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True)
    ])
    def get(self, request):
        task_id = request.query_params.get("task_id")
        if not task_id:
            return Response({"error": "task_id is required"}, status=400)

        result = AsyncResult(task_id)
        if not result.ready():
            return Response({"status": "Pending"}, status=202)

        if result.failed():
            return Response({"status": "Failed", "error": str(result.result)}, status=500)

        return Response({
            "status": "Completed",
            "data": result.result
        }, status=200)


class DownloadStructuredDocxView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def get(self, request):
        file_id = request.query_params.get("file_id")
        file_type = request.query_params.get("file_type")

        # Validate and fetch file
        file_obj = get_file_for_user(request.user, file_id)
        anonymize_obj = get_scoped_or_404(request.user, Anonymize, 'original_file', original_file=file_obj)

        docx_path = anonymize_obj.anonymized_structured_filepath.replace(".json", ".docx")
        if not os.path.exists(docx_path):
            return Response({"error": "Anonymized DOCX file not found."}, status=404)

        return FileResponse(open(docx_path, "rb"), content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


class DownloadRedactionReadyJSONView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(manual_parameters=[
        openapi.Parameter("file_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True)
    ])
    def get(self, request):
        file_id = request.query_params.get("file_id")
        instance = get_scoped_or_404(request.user, Anonymize, 'original_file', original_file_id=file_id, is_active=True, file_type="structured")

        with open(instance.anonymized_structured_filepath, "r", encoding="utf-8") as f:
            blocks = json.load(f)

        # Combine Presidio + SpaCy mappings
        mapping = {}
        mapping.update(instance.presidio_masking_map or {})
        mapping.update(instance.spacy_masking_map or {})

        redaction_targets = []
        for block in blocks:
            masked_text = block.get("text", "")
            if masked_text in mapping:
                real_text = mapping[masked_text]
                redaction_targets.append({
                    "text": real_text,
                    "page": block.get("metadata", {}).get("page_number", 1),
                    "coordinates": block.get("metadata", {}).get("coordinates", {}).get("points", [])
                })

        return Response(redaction_targets)


# document_anonymizer/views.py

from django.core.cache import cache
from datetime import datetime
from core.access import get_file_for_user, get_scoped_or_404, get_run_for_user, scope_by_file, scope_by_owner

class AnonymizationStatsView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter("client_name", openapi.IN_QUERY, type=openapi.TYPE_STRING),
            openapi.Parameter("project_id", openapi.IN_QUERY, type=openapi.TYPE_STRING),
            openapi.Parameter("service_id", openapi.IN_QUERY, type=openapi.TYPE_STRING),
            openapi.Parameter("date_from", openapi.IN_QUERY, type=openapi.TYPE_STRING, format=openapi.FORMAT_DATE),
            openapi.Parameter("date_to", openapi.IN_QUERY, type=openapi.TYPE_STRING, format=openapi.FORMAT_DATE),
        ],
        operation_description="Triggers computation of anonymization stats with optional filters."
    )
    def get(self, request):

        client_name = request.query_params.get("client_name")
        project_id = request.query_params.get("project_id")
        service_id = request.query_params.get("service_id")
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")

        cache_key = f"stats:{client_name}:{project_id}:{service_id}:{date_from}:{date_to}"
        cached_result = cache.get(cache_key)
        if cached_result:
            return Response({
                "cached": True,
                "data": cached_result
            })

        # launch Celery task
        task = compute_anonymization_stats_task.delay(
            client_name=client_name,
            project_id=project_id,
            service_id=service_id,
            date_from=date_from,
            date_to=date_to,
        )
        return Response({
            "task_id": task.id,
            "status": "Computation started"
        }, status=status.HTTP_202_ACCEPTED)


class AnonymizationStatsResultView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter("task_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True)
        ],
        operation_description="Fetches result of anonymization stats computation."
    )
    def get(self, request):
        task_id = request.query_params.get("task_id")
        if not task_id:
            return Response({"error": "task_id is required"}, status=400)

        result = AsyncResult(task_id)
        if not result.ready():
            return Response({"status": "Pending"}, status=202)

        if result.failed():
            return Response({"status": "Failed", "error": str(result.result)}, status=500)

        stats_data = result.result

        # Cache result for repeated queries
        cache_key = f"stats:{stats_data.get('client_name')}:{stats_data.get('project_id')}:{stats_data.get('service_id')}:{stats_data.get('date_from')}:{stats_data.get('date_to')}"
        cache.set(cache_key, stats_data, timeout=60 * 60)  # cache 1 hour

        return Response({
            "status": "Completed",
            "data": stats_data
        }, status=200)




class AnonymizationStatsHistoryView(generics.ListAPIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]
    serializer_class = AnonymizationStatsSerializer
    # pagination_class = PageNumberPagination  # Uses Django's default pagination
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        from core.access import client_name_q
        qs = AnonymizationStats.objects.filter(client_name_q(self.request.user)).order_by("-created_at")

        client_name = self.request.query_params.get("client_name")
        project_id = self.request.query_params.get("project_id")
        service_id = self.request.query_params.get("service_id")
        if client_name:
            qs = qs.filter(client_name=client_name)
        if project_id:
            qs = qs.filter(project_id=project_id)
        if service_id:
            qs = qs.filter(service_id=service_id)
        return qs





class AnonymizationInsightsView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Fetch or compute anonymization insights for the current user.",
        tags=["Anonymization Insights"],
        manual_parameters=[
            client_id_param,
            openapi.Parameter(
                "async", openapi.IN_QUERY,
                type=openapi.TYPE_BOOLEAN,
                description="Run computation asynchronously"
            ),
        ],
        responses={
            200: openapi.Response(
                description="Insights returned successfully."
            ),
            202: openapi.Response(
                description="Insights computation queued."
            ),
        }
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        user = get_user_from_client_id(client_id)

        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_403_FORBIDDEN)

        async_flag = request.query_params.get("async", "false").lower() == "true"

        if async_flag:
            task = generate_anonymization_insights_task.delay(user.id)
            return Response({
                "message": "Anonymization insights computation queued.",
                "task_id": task.id
            }, status=status.HTTP_202_ACCEPTED)

        # Otherwise compute synchronously
        from document_anonymizer.utils import calculate_anonymization_insights

        insights = calculate_anonymization_insights(user)

        return Response({
            "cached": False,
            "data": insights
        }, status=status.HTTP_200_OK)


class SupportedEntitiesView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Lists the actual spaCy & Presidio entity labels available at runtime.",
        tags=["Anonymization Insights"],
        manual_parameters=[
            client_id_param,
            openapi.Parameter("include_weights", openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False)
        ],
        responses={200: "Success"},
    )
    def get(self, request):
        from document_anonymizer.utils import list_supported_entities_runtime, list_supported_entities_with_weights
        payload = list_supported_entities_runtime()
        if str(request.query_params.get("include_weights", "false")).lower() == "true":
            payload["weights"] = list_supported_entities_with_weights()
        return Response(payload, status=200)


class CheckAnonymizationResultsView(APIView):
    """
    POST /api/v1/anonymizer/check-results/
    Body: {"file_ids": [1, 2, 3]}
    Returns anonymization processing status for each file.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    def post(self, request):
        file_ids = request.data.get("file_ids", [])
        if not file_ids:
            return Response({"error": "file_ids is required"}, status=status.HTTP_400_BAD_REQUEST)

        results = {}
        for fid in file_ids:
            anon = Anonymize.objects.filter(
                original_file__id=fid, status="Completed", is_active=True
            ).order_by("-created_at").first()
            if anon:
                results[fid] = {"status": "Completed", "anonymized_id": str(anon.id)}
            else:
                results[fid] = {"status": "NotProcessed"}

        return Response({"results": results}, status=status.HTTP_200_OK)

