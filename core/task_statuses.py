from django.shortcuts import get_object_or_404
from django.http import FileResponse, JsonResponse, Http404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from .models import Run, File, Metadata, EndpointResponseTable
import logging
import os
import uuid
from core.access import get_file_for_user, get_scoped_or_404, get_run_for_user, scope_by_file, scope_by_owner

logger = logging.getLogger(__name__)

# Swagger Parameters
client_id_param = openapi.Parameter(
    "X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True, description="Client ID provided at signup"
)
run_id_param = openapi.Parameter(
    "run_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Unique Run ID to track processing"
)
file_id_param = openapi.Parameter(
    "file_id", openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True, description="Unique File ID for retrieval"
)


class UploadStatusView(APIView):
    """
    Check the status of an upload using run_id
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Retrieve upload status by run_id.",
        tags=["Upload Status"],
        manual_parameters=[client_id_param, run_id_param],
        responses={200: "Success", 404: "Not Found"},
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        run_id = request.query_params.get("run_id")

        # Validate Run ID
        run = get_object_or_404(scope_by_owner(request.user, Run.objects.all()), run_id=run_id)

        # Fetch all files associated with this run
        files = File.objects.filter(run=run)

        file_list = [
            {
                "file_id": file.id,
                "filename": file.filename,
                "filepath": file.filepath,
                "file_size": file.file_size,
                "mime_type": file.file_type,
                "status": file.status,
            }
            for file in files
        ]

        return Response(
            {
                "run_id": run_id,
                "project_id": files.first().project_id if files else None,
                "service_id": files.first().service_id if files else None,
                "status": run.status,
                "files": file_list,
            },
            status=status.HTTP_200_OK,
        )


class MetadataStatusView(APIView):
    """
    Retrieve metadata details for a specific file
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Retrieve metadata details for a specific file using file_id.",
        tags=["Metadata Status"],
        manual_parameters=[client_id_param, file_id_param],
        responses={200: "Success", 404: "Not Found"},
    )
    def get(self, request):
        from core.access import get_file_for_user
        file = get_file_for_user(request.user, request.query_params.get("file_id"))
        run_id = request.query_params.get("run_id")
        association = None
        if run_id:
            try:
                parsed = uuid.UUID(str(run_id))
            except (ValueError, TypeError):
                raise Http404("Metadata run not found.")
            association = EndpointResponseTable.objects.filter(
                run_id=parsed, client_id=file.user_id, endpoint_name="MetadataView",
                response_data__file_id=file.pk).select_related("run").order_by("-created_at").first()
            if association is None:
                raise Http404("Metadata run not found.")
            state = association.status
            if state != "Completed":
                return Response({"status": state, "metadata": None,
                                 "error_message": (association.response_data or {}).get("error") if state == "Failed" else None},
                                status=202 if state in {"Pending", "Processing"} else 200)
            metadata_entry = Metadata.objects.filter(
                pk=(association.response_data or {}).get("metadata_id"), file=file).first()
            if metadata_entry is None:
                return Response({"status": "Failed", "metadata": None,
                                 "error_message": "The saved metadata is unavailable. Request extraction again."})
        else:
            metadata_entry = Metadata.objects.filter(file=file).order_by("-updated_at", "-pk").first()
            if metadata_entry is None:
                raise Http404("Metadata has not been extracted yet.")
        names = ("format", "md5_hash", "created_at", "updated_at", "title", "author", "subject", "keywords",
                 "creator", "producer", "creationdate", "moddate", "last_modified_by", "category", "content_status",
                 "revision", "trapped", "encryption", "page_count", "is_encrypted", "fonts", "page_rotation",
                 "pdfminer_info", "metadata_stream", "custom_metadata", "tagged", "userproperties", "suspects", "form",
                 "javascript", "pages", "encrypted", "page_size", "optimized", "pdf_version", "word_count")
        data = {"file_id": file.pk, "filename": file.filename, "file_size": file.file_size,
                **{name: getattr(metadata_entry, name) for name in names}}
        return Response({"status": "Completed", "metadata": data, "error_message": None} if run_id else data)
