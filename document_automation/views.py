import logging
import os

from django.db import models
from django.shortcuts import get_object_or_404
from django.contrib.auth import get_user_model
from django.http import JsonResponse
from oauth2_provider.models import Application
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework import generics
from rest_framework.permissions import AllowAny

from core.models import File
from core.utils import get_logical_path
from document_automation.models import AutomationTemplate, AutomationRun, AutomationResult, ClauseCategory, Clause, TemplateField
from document_automation.serializers import (
    AutomationTemplateSerializer,
    AutomationRunSerializer,
    AutomationResultSerializer,
    ClauseCategorySerializer,
    ClauseSerializer,
    TemplateFieldSerializer,
)
from document_automation.tasks import process_automation_task

from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi


logger = logging.getLogger(__name__)

User = get_user_model()

client_id_param = openapi.Parameter(
    "X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True, description="Client ID for authentication"
)
client_secret_param = openapi.Parameter(
    "X-Client-Secret", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True, description="Client Secret for authentication"
)
template_id_param = openapi.Parameter(
    "template_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Unique Template ID"
)
run_id_param = openapi.Parameter(
    "run_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Unique automation run ID"
)
file_id_param = openapi.Parameter(
    "file_id", openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True, description="Unique File ID"
)


def health_check(request):
    return JsonResponse({"status": "ok"}, status=200)


def get_user_from_client_id(client_id):
    try:
        application = Application.objects.get(client_id=client_id)
        return application.user
    except Application.DoesNotExist:
        return None


class AutomationTemplateListCreateView(generics.ListCreateAPIView):
    queryset = AutomationTemplate.objects.all()
    serializer_class = AutomationTemplateSerializer
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="List all automation templates.",
        tags=["Automation Templates"],
        manual_parameters=[client_id_param, client_secret_param],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @swagger_auto_schema(
        operation_description="Create a new automation template.",
        tags=["Automation Templates"],
        manual_parameters=[client_id_param, client_secret_param],
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)


class AutomationTemplateDetailView(generics.RetrieveAPIView):
    queryset = AutomationTemplate.objects.all()
    serializer_class = AutomationTemplateSerializer
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Get details of a specific automation template.",
        tags=["Automation Templates"],
        manual_parameters=[client_id_param, client_secret_param, template_id_param],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class SubmitAutomationAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Submit a template for document automation.",
        tags=["Document Automation"],
        manual_parameters=[
            client_id_param, client_secret_param, template_id_param,
            openapi.Parameter("input_data", openapi.IN_BODY, type=openapi.TYPE_OBJECT, required=True, description="JSON data for template rendering"),
        ],
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        template_id = request.query_params.get("template_id")
        input_data = request.data.get("input_data", {})

        if not all([client_id, client_secret, template_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        template = get_object_or_404(AutomationTemplate, id=template_id)

        if template.file.user != user:
            raise PermissionDenied("You are not authorized to use this template.")

        if not os.path.exists(template.file.filepath):
            return Response({"error": "Template file not found."}, status=status.HTTP_404_NOT_FOUND)

        bulk_count = request.data.get("bulk_count", 1)
        bulk_data = request.data.get("bulk_data", None)
        clause_ids = request.data.get("clause_ids", None)
        output_format = request.data.get("output_format", "DOCX")

        automation_run = AutomationRun.objects.create(
            project_id=template.project_id,
            service_id=template.service_id,
            client_name=user.username if user and user.username else user.email,
            template=template,
            input_data=input_data,
            status="Pending",
            bulk_count=bulk_count,
            bulk_data=bulk_data,
            clause_ids=clause_ids,
            output_format=output_format,
        )

        process_automation_task.delay(str(automation_run.id))

        response_data = {
            "run_id": str(automation_run.id),
            "template_id": str(template.id),
            "status": "Pending",
            "bulk_count": bulk_count,
            "output_format": output_format,
        }

        return Response(response_data, status=status.HTTP_202_ACCEPTED)


class CheckAutomationStatusAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Check the status of a document automation run.",
        tags=["Document Automation"],
        manual_parameters=[client_id_param, client_secret_param, run_id_param],
    )
    def get(self, request):
        run_id = request.query_params.get("run_id")

        if not run_id:
            return Response({"error": "Missing `run_id` parameter"}, status=status.HTTP_400_BAD_REQUEST)

        automation_run = get_object_or_404(AutomationRun, id=run_id)

        result_data = {
            "run_id": str(automation_run.id),
            "template_id": str(automation_run.template.id),
            "status": automation_run.status,
            "error_message": automation_run.error_message,
            "created_at": automation_run.created_at,
            "updated_at": automation_run.updated_at,
            "bulk_count": automation_run.bulk_count,
            "output_format": automation_run.output_format,
        }

        return Response(
            result_data,
            status=status.HTTP_200_OK if automation_run.status == "Completed" else status.HTTP_202_ACCEPTED,
        )


class DownloadAutomationResultAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Download a generated document by providing the `file_id`.",
        tags=["Document Automation"],
        manual_parameters=[client_id_param, client_secret_param, file_id_param],
    )
    def get(self, request):
        file_id = request.query_params.get("file_id")

        if not file_id:
            return Response({"error": "Missing `file_id` parameter"}, status=status.HTTP_400_BAD_REQUEST)

        file_instance = get_object_or_404(File, id=file_id)

        if not os.path.exists(file_instance.filepath):
            return Response({"error": "Generated file not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response(
            {"file_id": file_id, "filepath": get_logical_path(file_instance.filepath, file_instance.filename), "filename": file_instance.filename, "status": "Ready for download"},
            status=status.HTTP_200_OK,
        )


class ClauseCategoryListCreateView(generics.ListCreateAPIView):
    queryset = ClauseCategory.objects.all()
    serializer_class = ClauseCategorySerializer
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="List or create clause categories.",
        tags=["Clause Management"],
        manual_parameters=[client_id_param, client_secret_param],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @swagger_auto_schema(
        operation_description="Create a new clause category.",
        tags=["Clause Management"],
        manual_parameters=[client_id_param, client_secret_param],
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

    def get_queryset(self):
        qs = super().get_queryset()
        project_id = self.request.query_params.get('project_id')
        if project_id:
            qs = qs.filter(project_id=project_id)
        return qs


class ClauseListCreateView(generics.ListCreateAPIView):
    queryset = Clause.objects.all()
    serializer_class = ClauseSerializer
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="List or create clauses.",
        tags=["Clause Management"],
        manual_parameters=[client_id_param, client_secret_param],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @swagger_auto_schema(
        operation_description="Create a new clause.",
        tags=["Clause Management"],
        manual_parameters=[client_id_param, client_secret_param],
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

    def get_queryset(self):
        qs = super().get_queryset()
        category_id = self.request.query_params.get('category')
        project_id = self.request.query_params.get('project_id')
        service_id = self.request.query_params.get('service_id')
        if category_id:
            qs = qs.filter(category_id=category_id)
        if project_id:
            qs = qs.filter(project_id=project_id)
        if service_id:
            qs = qs.filter(service_id=service_id)
        return qs


class ClauseDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = Clause.objects.all()
    serializer_class = ClauseSerializer
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Get details of a specific clause.",
        tags=["Clause Management"],
        manual_parameters=[client_id_param, client_secret_param],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @swagger_auto_schema(
        operation_description="Update a specific clause.",
        tags=["Clause Management"],
        manual_parameters=[client_id_param, client_secret_param],
    )
    def put(self, request, *args, **kwargs):
        return super().put(request, *args, **kwargs)

    @swagger_auto_schema(
        operation_description="Delete a specific clause.",
        tags=["Clause Management"],
        manual_parameters=[client_id_param, client_secret_param],
    )
    def delete(self, request, *args, **kwargs):
        return super().delete(request, *args, **kwargs)


class TemplateFieldExtractView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Extract and return template fields for a given template.",
        tags=["Template Fields"],
        manual_parameters=[client_id_param, client_secret_param, template_id_param],
    )
    def get(self, request):
        template_id = request.query_params.get("template_id")

        if not template_id:
            return Response({"error": "Missing `template_id` parameter"}, status=status.HTTP_400_BAD_REQUEST)

        template = get_object_or_404(AutomationTemplate, id=template_id)
        fields = TemplateField.objects.filter(template=template).order_by('order')
        serializer = TemplateFieldSerializer(fields, many=True)

        return Response(serializer.data, status=status.HTTP_200_OK)


class BulkSubmitAutomationAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Submit a bulk document automation request.",
        tags=["Document Automation"],
        manual_parameters=[
            client_id_param, client_secret_param, template_id_param,
            openapi.Parameter("bulk_data", openapi.IN_BODY, type=openapi.TYPE_ARRAY, items=openapi.Items(type=openapi.TYPE_OBJECT), required=True, description="Array of input_data dicts for bulk generation"),
        ],
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        template_id = request.query_params.get("template_id")
        bulk_data = request.data.get("bulk_data", [])
        clause_ids = request.data.get("clause_ids", None)
        output_format = request.data.get("output_format", "DOCX")

        if not all([client_id, client_secret, template_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        if not isinstance(bulk_data, list) or len(bulk_data) == 0:
            return Response({"error": "`bulk_data` must be a non-empty array"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        template = get_object_or_404(AutomationTemplate, id=template_id)

        if template.file.user != user:
            raise PermissionDenied("You are not authorized to use this template.")

        if not os.path.exists(template.file.filepath):
            return Response({"error": "Template file not found."}, status=status.HTTP_404_NOT_FOUND)

        automation_run = AutomationRun.objects.create(
            project_id=template.project_id,
            service_id=template.service_id,
            client_name=user.username if user and user.username else user.email,
            template=template,
            input_data=bulk_data[0] if bulk_data else {},
            status="Pending",
            bulk_count=len(bulk_data),
            bulk_data=bulk_data,
            clause_ids=clause_ids,
            output_format=output_format,
        )

        process_automation_task.delay(str(automation_run.id))

        response_data = {
            "run_id": str(automation_run.id),
            "template_id": str(template.id),
            "status": "Pending",
            "bulk_count": len(bulk_data),
            "output_format": output_format,
        }

        return Response(response_data, status=status.HTTP_202_ACCEPTED)
