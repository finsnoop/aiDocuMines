# document_structures/views.py

import uuid
import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from oauth2_provider.contrib.rest_framework import OAuth2Authentication, TokenHasReadWriteScope
from custom_authentication.permissions import IsClientOrAdminOrSuperUser
from oauth2_provider.models import Application
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi

from core.models import File
from document_structures import models, tasks, serializers
from core.access import get_file_for_user, get_scoped_or_404, get_run_for_user, scope_by_file, scope_by_owner

logger = logging.getLogger(__name__)

# Swagger reusable params
client_id_param = openapi.Parameter("X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True)
client_secret_param = openapi.Parameter("X-Client-Secret", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True)
file_id_param = openapi.Parameter("file_id", openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True)
partition_strategy_param = openapi.Parameter(
    "partition_strategy",
    openapi.IN_QUERY,
    type=openapi.TYPE_STRING,
    enum=["partition_pdf", "partition_text", "partition_auto"],
    required=False,
    description="Overrides default partitioning strategy. Defaults to auto."
)

run_1_param = openapi.Parameter("run_1", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="UUID of first DocumentStructureRun")
run_2_param = openapi.Parameter("run_2", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="UUID of second DocumentStructureRun")


def get_user_from_client_id(client_id):
    try:
        application = Application.objects.get(client_id=client_id)
        return application.user
    except Application.DoesNotExist:
        return None


class SubmitDocumentPartitionAPIView(APIView):
    """
    Starts document partitioning via unstructured for a specific file.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        manual_parameters=[
            client_id_param,
            client_secret_param,
            file_id_param,
            partition_strategy_param,
            openapi.Parameter("project_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description="Project ID for report registration"),
            openapi.Parameter("service_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description="Service ID for report registration"),
            openapi.Parameter("generate_report", openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False, description="Generate HTML report on completion"),
        ],
        operation_description="Starts document partitioning using the Unstructured library for PDF, DOCX or text files."
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        file_id = request.query_params.get("file_id")
        partition_strategy = request.query_params.get("partition_strategy", "partition_auto")

        # New parameters for report generation
        project_id = request.query_params.get("project_id")
        service_id = request.query_params.get("service_id")
        generate_report = request.query_params.get("generate_report", "false").lower() == "true"

        if not all([client_id, client_secret, file_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_403_FORBIDDEN)

        file_instance = get_file_for_user(request.user, file_id)

        if str(file_instance.user.id) != str(user.id):
            return Response({"error": "You are not authorized to process this file."}, status=status.HTTP_403_FORBIDDEN)

        force = request.query_params.get("force", "false").lower() == "true"

        existing = models.DocumentStructureRun.objects.filter(
            file=file_instance,
            user=user,
            partition_strategy=partition_strategy,
            status="Completed"
        ).first()

        if existing and not force:
            return Response({
                "message": "This file has already been processed.",
                "document_structure_run_id": str(existing.id),
                "status": existing.status
            }, status=status.HTTP_200_OK)

        # Optional safety: delete old run if forcing
        if existing and force:
            logger.warning(f"⚠️ Force rerun requested. Deleting previous run {existing.id}")
            existing.delete()

        # Create new structure run
        ds_run = models.DocumentStructureRun.objects.create(
            id=str(uuid.uuid4()),
            run=file_instance.run,
            file=file_instance,
            user=user,
            partition_strategy=partition_strategy,
            status="Pending",
        )

        # Pass report generation parameters to the task
        tasks.run_document_partition_task.delay(
            str(ds_run.id),
            store_embeddings=True,
            project_id=project_id,
            service_id=service_id,
            generate_report=generate_report
        )

        return Response({
            "document_structure_run_id": str(ds_run.id),
            "file_id": str(file_instance.id),
            "status": "Processing"
        }, status=status.HTTP_202_ACCEPTED)


class DocumentStructureRunStatusAPIView(APIView):
    """
    Checks status + optionally retrieves extracted elements for a DocumentStructureRun
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        manual_parameters=[
            client_id_param,
            client_secret_param,
            openapi.Parameter("document_structure_run_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True),
            openapi.Parameter("include_elements", openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, required=False, description="If true, returns extracted elements.")
        ]
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        ds_run_id = request.query_params.get("document_structure_run_id")
        include_elements = request.query_params.get("include_elements", "false").lower() == "true"

        if not all([client_id, client_secret, ds_run_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_403_FORBIDDEN)

        ds_run = get_object_or_404(models.DocumentStructureRun, id=ds_run_id)

        if str(ds_run.user.id) != str(user.id):
            return Response({"error": "Not authorized to view this document structure run."}, status=status.HTTP_403_FORBIDDEN)

        serialized = serializers.DocumentStructureRunSerializer(ds_run)
        data = serialized.data

        if include_elements:
            elements_qs = models.DocumentElement.objects.filter(run=ds_run).order_by("order")
            data["elements"] = serializers.DocumentElementSerializer(elements_qs, many=True).data

        return Response(data, status=status.HTTP_200_OK)


class SubmitDocumentComparisonAPIView(APIView):
    """
    Starts a comparison between two DocumentStructureRuns.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        manual_parameters=[
            client_id_param,
            client_secret_param,
            run_1_param,
            run_2_param,
        ],
        operation_description="Starts a comparison of two extracted document structures."
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        run_1_id = request.query_params.get("run_1")
        run_2_id = request.query_params.get("run_2")

        if not all([client_id, client_secret, run_1_id, run_2_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_403_FORBIDDEN)

        run_1 = get_object_or_404(models.DocumentStructureRun, id=run_1_id)
        run_2 = get_object_or_404(models.DocumentStructureRun, id=run_2_id)

        if str(run_1.user.id) != str(user.id) or str(run_2.user.id) != str(user.id):
            return Response({"error": "Not authorized to compare these documents."}, status=status.HTTP_403_FORBIDDEN)

        # Check if comparison already exists
        existing = models.DocumentComparison.objects.filter(
            run_1=run_1,
            run_2=run_2,
            status="Completed"
        ).first()

        force = request.query_params.get("force", "false").lower() == "true"

        if existing and not force:
            return Response({
                "message": "Comparison already exists.",
                "comparison_id": str(existing.id),
                "status": existing.status
            }, status=status.HTTP_200_OK)

        comparison = models.DocumentComparison.objects.create(
            run_1=run_1,
            run_2=run_2,
            status="Pending"
        )

        tasks.run_document_comparison_task.delay(str(comparison.id))

        return Response({
            "comparison_id": str(comparison.id),
            "status": "Processing"
        }, status=status.HTTP_202_ACCEPTED)


class DocumentComparisonStatusAPIView(APIView):
    """
    Fetches result/status for a document comparison.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        manual_parameters=[
            client_id_param,
            client_secret_param,
            openapi.Parameter("comparison_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True)
        ]
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        comparison_id = request.query_params.get("comparison_id")

        if not all([client_id, client_secret, comparison_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_403_FORBIDDEN)

        comparison = get_object_or_404(models.DocumentComparison, id=comparison_id)

        if str(comparison.run_1.user.id) != str(user.id) or str(comparison.run_2.user.id) != str(user.id):
            return Response({"error": "Not authorized to view this comparison."}, status=status.HTTP_403_FORBIDDEN)

        serialized = serializers.DocumentComparisonSerializer(comparison)
        return Response(serialized.data, status=status.HTTP_200_OK)

