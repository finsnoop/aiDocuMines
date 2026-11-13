from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from django.http import FileResponse
from django.contrib.auth import get_user_model
from oauth2_provider.models import Application
from core.models import File
from document_redlining.models import RedliningRun, RedliningResult
from document_redlining.tasks import perform_redlining_task
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
import logging
import os
import mimetypes


logger = logging.getLogger(__name__)

User = get_user_model()

client_id_param = openapi.Parameter(
    "X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True, description="Client ID for authentication"
)
client_secret_param = openapi.Parameter(
    "X-Client-Secret", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True, description="Client Secret for authentication"
)
file_id_param = openapi.Parameter(
    "file_id", openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True, description="Unique File ID of the original document"
)
comparison_file_id_param = openapi.Parameter(
    "comparison_file_id", openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True, description="Unique File ID of the document to compare against"
)
run_id_param = openapi.Parameter(
    "run_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Unique redlining run ID"
)
output_type_param = openapi.Parameter(
    "output_type", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description="Output format: html, docx, or pdf"
)


def health_check(request):
    from django.http import JsonResponse
    return JsonResponse({"status": "ok"}, status=200)


def get_user_from_client_id(client_id):
    """Retrieves the User associated with a given `client_id` from OAuth2 Application."""
    try:
        application = Application.objects.get(client_id=client_id)
        return application.user
    except Application.DoesNotExist:
        return None


class SubmitRedliningAPIView(APIView):
    """
    Submit two files for redlining/comparison and track using `run_id`.
    """

    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Submit two files for document comparison (redlining).",
        tags=["Redlining"],
        manual_parameters=[
            client_id_param, client_secret_param, file_id_param, comparison_file_id_param
        ],
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        file_id = request.query_params.get("file_id")
        comparison_file_id = request.query_params.get("comparison_file_id")

        if not all([client_id, client_secret, file_id, comparison_file_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        file_instance = get_object_or_404(File, id=file_id)
        comparison_instance = get_object_or_404(File, id=comparison_file_id)

        if file_instance.user != user or comparison_instance.user != user:
            raise PermissionDenied("You are not authorized to compare these files.")

        if not os.path.exists(file_instance.filepath):
            return Response({"error": "Original file not found."}, status=status.HTTP_404_NOT_FOUND)

        if not os.path.exists(comparison_instance.filepath):
            return Response({"error": "Comparison file not found."}, status=status.HTTP_404_NOT_FOUND)

        redlining_run = RedliningRun.objects.create(
            project_id=file_instance.project_id,
            service_id=file_instance.service_id,
            status="Processing",
            client_name=user.username if user and user.username else user.email
        )

        perform_redlining_task.delay(file_id, comparison_file_id, str(redlining_run.id))

        response_data = {
            "run_id": str(redlining_run.id),
            "file_id": str(file_instance.id),
            "comparison_file_id": str(comparison_instance.id),
            "status": "Processing"
        }

        return Response(response_data, status=status.HTTP_202_ACCEPTED)


class CheckRedliningStatusAPIView(APIView):
    """
    Check the redlining status using `run_id`.
    """

    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Check the status of a redlining run using `run_id`.",
        tags=["Redlining"],
        manual_parameters=[client_id_param, client_secret_param, run_id_param],
    )
    def get(self, request):
        run_id = request.query_params.get("run_id")

        if not run_id:
            return Response({"error": "Missing `run_id` parameter"}, status=status.HTTP_400_BAD_REQUEST)

        run = get_object_or_404(RedliningRun, id=run_id)
        results = RedliningResult.objects.filter(run=run).first()

        response_data = {
            "run_id": str(run.id),
            "status": run.status,
            "error_message": run.error_message,
        }

        if results and run.status == "Completed":
            response_data["result_id"] = str(results.id)
            response_data["diff_html"] = results.diff_html
            response_data["comparison_stats"] = results.comparison_stats

        status_code = status.HTTP_200_OK if run.status == "Completed" else status.HTTP_202_ACCEPTED
        return Response(response_data, status=status_code)


class DownloadRedliningResultAPIView(APIView):
    """
    Download the redlining diff result by providing the `file_id`.
    Supports output_type: html (default), docx, or pdf.
    """

    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Download the redlining diff result by providing the original `file_id`.",
        tags=["Redlining"],
        manual_parameters=[client_id_param, client_secret_param, file_id_param, output_type_param],
    )
    def get(self, request):
        file_id = request.query_params.get("file_id")
        output_type = request.query_params.get("output_type", "html")

        if not file_id:
            return Response({"error": "Missing `file_id` parameter"}, status=status.HTTP_400_BAD_REQUEST)

        if output_type not in ("html", "docx", "pdf"):
            return Response({"error": "Invalid output_type. Must be html, docx, or pdf."}, status=status.HTTP_400_BAD_REQUEST)

        result = get_object_or_404(RedliningResult, original_file__id=file_id)

        if result.status != "Completed":
            return Response({"error": "Redlining result not yet completed."}, status=status.HTTP_202_ACCEPTED)

        if output_type == "docx":
            path = result.redline_docx_path
            if path and os.path.exists(path):
                content_type, _ = mimetypes.guess_type(path)
                response = FileResponse(
                    open(path, "rb"),
                    content_type=content_type or "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
                response["Content-Disposition"] = f'attachment; filename="{os.path.basename(path)}"'
                return response
            return Response({"error": "DOCX redline output not available."}, status=status.HTTP_404_NOT_FOUND)

        if output_type == "pdf":
            path = result.redline_pdf_path
            if path and os.path.exists(path):
                response = FileResponse(
                    open(path, "rb"),
                    content_type="application/pdf",
                )
                response["Content-Disposition"] = f'attachment; filename="{os.path.basename(path)}"'
                return response
            return Response({"error": "PDF redline output not available."}, status=status.HTTP_404_NOT_FOUND)

        if result.diff_html:
            return Response(
                {
                    "file_id": file_id,
                    "diff_html": result.diff_html,
                    "status": "Ready",
                },
                status=status.HTTP_200_OK
            )

        return Response({"error": "Redlining result not found."}, status=status.HTTP_404_NOT_FOUND)
