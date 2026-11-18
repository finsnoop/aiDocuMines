from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.generics import GenericAPIView
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from oauth2_provider.models import Application
from core.models import File
from core.utils import get_logical_path
from document_translation.models import TranslationRun, TranslationFile
from document_translation.utils import get_language_name
import logging
import os
import json
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication

from rest_framework.permissions import AllowAny

# Swagger Imports
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from core.access import get_file_for_user, get_scoped_or_404, get_run_for_user, scope_by_file, scope_by_owner

logger = logging.getLogger(__name__)

# Define Swagger parameters
client_id_param = openapi.Parameter(
    "X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True,
    description="Client ID for authentication."
)
client_secret_param = openapi.Parameter(
    "X-Client-Secret", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True,
    description="Client Secret for authentication."
)
file_id_param = openapi.Parameter(
    "file_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False,
    description="Unique `file_id` to track translation."
)
run_id_param = openapi.Parameter(
    "run_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False,
    description="Unique `run_id` to track translation."
)




class TranslationTaskStatusView(APIView):
    """
    Retrieve the status of a document translation using `run_id` or `file_id`.
    Prioritizes `run_id` if both are provided to avoid ambiguous results.
    """

    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Check the status of a document translation using `file_id` or `run_id`. If both are provided, `run_id` is prioritized.",
        tags=["Translation Status"],
        manual_parameters=[client_id_param, client_secret_param, file_id_param, run_id_param],
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        file_id = request.query_params.get("file_id")
        run_id = request.query_params.get("run_id") or request.query_params.get("translation_run_id")

        if not file_id and not run_id:
            return Response({"error": "Missing required parameters"}, status=400)

        user = request.user
        if not user:
            logger.error(f"Invalid client ID: {client_id}")
            return Response({"error": "Invalid client ID"}, status=403)

        run_instance = None
        file_instance = None

        # Prefer run_id
        if run_id:
            try:
                run_instance = get_run_for_user(request.user, TranslationRun, run_id)
            except TranslationRun.DoesNotExist:
                return Response({"error": "No translation process found for the provided run_id"}, status=404)

        # Resolve via file_id if needed
        if not run_instance and file_id:
            file_instance = get_file_for_user(request.user, file_id)
            tf_for_file = scope_by_file(request.user, TranslationFile.objects.filter(original_file=file_instance), 'original_file').order_by("-created_at").first()
            if tf_for_file:
                run_instance = tf_for_file.run

        if not run_instance:
            return Response({"error": "No translation process found for the provided parameters"}, status=404)

        # Prefer the translation file matching file_id if provided
        if file_instance:
            translation_file = scope_by_file(request.user, TranslationFile.objects.filter(
                run=run_instance, original_file=file_instance
            ), 'original_file').order_by("-created_at").first()
        else:
            translation_file = scope_by_file(request.user, TranslationFile.objects.filter(run=run_instance),
                                             'original_file').order_by("-created_at").first()

        if not translation_file:
            return Response({"error": "No translation file found for the provided details"}, status=404)

        response_data = {
            "translation_run_id": str(run_instance.id),
            "original_file_id": str(translation_file.original_file.id),
            "translated_file_id": str(translation_file.id),
            "from_language": run_instance.from_language,
            "to_language": run_instance.to_language,
            "status": run_instance.status,
            "error_message": run_instance.error_message if run_instance.status == "Failed" else None,
            "project_id": translation_file.original_file.project_id,
            "service_id": translation_file.original_file.service_id,
            "client_name": run_instance.client_name,
            "original_file_path": get_logical_path(translation_file.original_file.filepath),
            "translated_file_path": get_logical_path(translation_file.translated_filepath),
            "created_at": translation_file.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": translation_file.updated_at.strftime("%Y-%m-%d %H:%M:%S"),
        }

        # ONLY translated files for THIS original file (all languages)
        qs = TranslationFile.objects.filter(
            original_file=translation_file.original_file
        ).select_related("run").order_by("-created_at")

        # If you want only the current run’s language, use:
        # qs = qs.filter(run__to_language=run_instance.to_language)

        registered_outputs = []
        for tf in qs:
            if not tf.translated_filepath:
                continue
            
            # Find the File record for this translated file
            file_record = File.objects.filter(filepath=tf.translated_filepath, user=tf.original_file.user).first()
            
            registered_outputs.append({
                "translated_file_id": str(tf.id),
                "file_id": str(file_record.id) if file_record else None,
                "language": tf.run.to_language,
                "language_name": get_language_name(tf.run.to_language),
                "filename": os.path.basename(tf.translated_filepath) if tf.translated_filepath else None,
                "filepath": get_logical_path(tf.translated_filepath),
                "status": tf.run.status,
                # Optional convenience link if you expose a download route:
                # "download_url": f"/api/v1/translation/download/?file_id={tf.original_file.id}&file_type=translated&language={tf.run.to_language}",
            })

        response_data["registered_outputs"] = registered_outputs

        logger.info(f"[Translation Status] Responded for run_id={run_instance.id}, file_id={file_id or 'N/A'}")
        return Response(response_data, status=200)

























class TranslationFileDownloadView(APIView):
    """
    API endpoint to download translated files.
    """

    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]  # Require OAuth2 authentication

    @swagger_auto_schema(
        operation_description="Download the translated file using `file_id` and optional `language` filter.",
        tags=["Translation Download"],
        manual_parameters=[
            client_id_param, client_secret_param,
            openapi.Parameter("file_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True,
                              description="Unique `file_id` to retrieve the translated file."),
            openapi.Parameter("file_type", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True,
                              enum=["original", "translated"],
                              description="Specify 'original' to download the original file or 'translated' for the translated file."),
            openapi.Parameter("language", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True,
                              description="Specify the target language code to download the correct translated document."),
        ],
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        file_id = request.query_params.get("file_id")
        file_type = request.query_params.get("file_type")
        language = request.query_params.get("language")  # Required parameter to fetch specific language translation

        if not file_id or not file_type or not language:
            return Response({"error": "Missing required parameters"}, status=400)

        # Validate client ID and retrieve user
        user = request.user
        if not user:
            return Response({"error": "Invalid client ID"}, status=403)

        # Fetch the translation file filtered by `file_id` & `language`
        source = get_file_for_user(request.user, file_id, "download")
        translation_file = TranslationFile.objects.filter(
            original_file=source, run__to_language=language, status="Completed"
        ).order_by("-created_at").first()

        if not translation_file:
            return Response({"error": f"No translated file found for file_id={file_id} in language={language}"}, status=404)

        # Determine the file path
        if file_type == "original":
            file_path = translation_file.original_file.filepath
        elif file_type == "translated":
            file_path = translation_file.translated_filepath
        else:
            return Response({"error": "Invalid file_type. Use 'original' or 'translated'."}, status=400)

        # Ensure the file exists before attempting to serve it
        if not file_path or not os.path.exists(file_path):
            logger.error(f"File not found: {file_path}")
            return Response({"error": "File not found."}, status=404)

        logger.info(f"Serving file: {file_path}")
        return FileResponse(open(file_path, "rb"), as_attachment=True)
