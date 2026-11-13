from rest_framework.views import APIView
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from oauth2_provider.models import Application
from core.models import File
from core.utils import get_logical_path
from document_ocr.models import OCRRun, OCRFile
import logging
import os
from django.http import FileResponse
from rest_framework.permissions import AllowAny

# OAuth2 Authentication Imports
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication
from rest_framework.permissions import IsAuthenticated  # ✅ Ensures authentication

# Swagger Imports
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from core.access import get_file_for_user, get_scoped_or_404, get_run_for_user, scope_by_file, scope_by_owner

logger = logging.getLogger(__name__)

# ✅ Define Swagger parameters for authentication
client_id_param = openapi.Parameter(
    "X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True,
    description="Client ID provided at signup"
)
client_secret_param = openapi.Parameter(
    "X-Client-Secret", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True,
    description="Client Secret for authentication"
)
file_id_param = openapi.Parameter(
    "file_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False,
    description="Unique `file_id` to track OCR."
)
run_id_param = openapi.Parameter(
    "run_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False,
    description="Unique `run_id` to track OCR."
)

def get_user_from_client_id(client_id):
    """Retrieves the User associated with a given `client_id` from OAuth2 Application."""
    try:
        application = Application.objects.get(client_id=client_id)
        return application.user
    except Application.DoesNotExist:
        return None

class OCRTaskStatusView(APIView):
    """
    Retrieve the status of an OCR process using `file_id` or `run_id`.
    """

    authentication_classes = [OAuth2Authentication]  # ✅ Require OAuth2 token authentication
    permission_classes = [TokenHasReadWriteScope]  # ✅ Require valid token with proper scope

    @swagger_auto_schema(
        operation_description="Check the status of an OCR process using `file_id` or `run_id`.",
        tags=["OCR Status"],
        manual_parameters=[
            client_id_param, client_secret_param, file_id_param, run_id_param
        ],
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        file_id = request.query_params.get("file_id")
        run_id = request.query_params.get("run_id")

        # ✅ Ensure authentication headers are present
        if not client_id or not client_secret:
            return Response({"error": "Missing authentication parameters"}, status=400)

        # ✅ Validate client ID and retrieve user
        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=403)

        # ✅ Fetch `OCRRun` by `run_id` (if provided)
        run_instance = None
        if run_id:
            run_instance = get_run_for_user(request.user, OCRRun, run_id)

        # ✅ Fetch `File` by `file_id` (if provided)
        file_instance = None
        if file_id:
            file_instance = get_file_for_user(request.user, file_id)
            # ✅ Find corresponding OCR run using `file_id`
            ocr_file = scope_by_file(request.user, OCRFile.objects.filter(original_file=file_instance), 'original_file').first()
            if ocr_file:
                run_instance = ocr_file.run

        # ✅ Ensure that an `OCRRun` instance is found
        if not run_instance:
            return Response({"error": "No OCR process found for the provided file_id or run_id"}, status=404)

        # ✅ Fetch OCR file details
        ocr_file = OCRFile.objects.filter(run=run_instance).first()
        if not ocr_file:
            return Response({"error": "No OCR file found for the provided details"}, status=404)

        # 🆕 Fetch all registered File entries for this run (except original)
        registered_outputs = [
            {
                "id": f["id"],
                "filename": f["filename"],
                "filepath": get_logical_path(f["filepath"], f["filename"]),
            }
            for f in File.objects.filter(run=ocr_file.original_file.run)
            .exclude(id=ocr_file.original_file.id)
            .values("id", "filename", "filepath")
        ]

        # ✅ Ensure that file paths exist and are formatted correctly
        response_data = {
            "ocr_run_id": str(run_instance.id),
            "original_file_id": str(ocr_file.original_file.id),
            "ocr_file_id": str(ocr_file.id),
            "status": run_instance.status,
            "ocr_option": run_instance.ocr_option,
            "error_message": run_instance.error_message if run_instance.status == "Failed" else None,
            "project_id": ocr_file.original_file.project_id,
            "service_id": ocr_file.original_file.service_id,
            "client_name": run_instance.client_name,
            "original_file_path": get_logical_path(ocr_file.original_file.filepath, ocr_file.original_file.filename),
            "ocr_file_path": get_logical_path(ocr_file.ocr_filepath) if ocr_file.ocr_filepath else "N/A",
            "formatted_docx_path": get_logical_path(ocr_file.docx_path) if ocr_file.docx_path else "N/A",
            "raw_docx_path": get_logical_path(ocr_file.raw_docx_path) if ocr_file.raw_docx_path else "N/A",
            "created_at": ocr_file.created_at,
            "updated_at": ocr_file.updated_at,
            "registered_outputs": registered_outputs  # ✅ Added
        }

        return Response(response_data, status=200)

class OCRFileDownloadView(APIView):
    """
    Download the processed OCR files.
    """

    permission_classes = [AllowAny]  # Adjust permission as needed

    @swagger_auto_schema(
        operation_description="Download a specific OCR file by providing the `file_type` and `file_id`.",
        tags=["OCR File Download"],
        manual_parameters=[
            openapi.Parameter("file_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True,
                              description="The `file_id` of the original file."),
            openapi.Parameter("file_type", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True,
                              enum=["original", "ocr_pdf", "raw_docx", "formatted_docx"],
                              description="The type of file to download: 'original', 'ocr_pdf', 'raw_docx', or 'formatted_docx'."),
        ],
        responses={
            200: "File successfully retrieved.",
            400: "Bad Request - Missing required parameters",
            404: "Not Found - The requested file does not exist",
            500: "Internal Server Error - Unexpected issue occurred.",
        },
    )
    def get(self, request):
        file_id = request.query_params.get("file_id")
        file_type = request.query_params.get("file_type")

        if not file_id or not file_type:
            return Response({"error": "Missing required parameters"}, status=400)

        ocr_file = get_scoped_or_404(request.user, OCRFile, 'original_file', original_file__id=file_id)

        # ✅ Determine which file to serve based on the requested file_type
        file_path_map = {
            "original": ocr_file.original_file.filepath,
            "ocr_pdf": ocr_file.ocr_filepath,
            "raw_docx": ocr_file.raw_docx_path,
            "formatted_docx": ocr_file.docx_path,
        }

        file_path = file_path_map.get(file_type)

        if not file_path or not os.path.exists(file_path):
            return Response({"error": f"Requested {file_type} file does not exist."}, status=404)

        try:
            response = FileResponse(open(file_path, "rb"), as_attachment=True)
            response["Content-Disposition"] = f'attachment; filename="{os.path.basename(file_path)}"'
            return response
        except Exception as e:
            logger.error(f"❌ Error serving file {file_path}: {e}")
            return Response({"error": "Failed to retrieve file."}, status=500)

