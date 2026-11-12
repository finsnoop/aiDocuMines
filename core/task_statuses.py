from django.shortcuts import get_object_or_404
from django.http import FileResponse, JsonResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from oauth2_provider.contrib.rest_framework import OAuth2Authentication, TokenHasReadWriteScope
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from .models import Run, File, Metadata, EndpointResponseTable
import logging
import os
import uuid
from core.access import get_file_for_user, get_scoped_or_404, get_run_for_user, scope_by_file, scope_by_owner

logger = logging.getLogger(__name__)

# ✅ Swagger Parameters
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
    ✅ Check the status of an upload using run_id
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

        # ✅ Validate Run ID
        run = get_object_or_404(scope_by_owner(request.user, Run.objects.all()), run_id=run_id)

        # ✅ Fetch all files associated with this run
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
    ✅ Retrieve metadata details for a specific file
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
        client_id = request.headers.get("X-Client-ID")
        file_id = request.query_params.get("file_id")

        # ✅ Fetch metadata entry for the given file_id
        metadata_entry = get_object_or_404(Metadata, file_id=file_id)

        return Response(
            {
                "file_id": metadata_entry.file.id,
                "filename": metadata_entry.file.filename,
                "file_size": metadata_entry.file.file_size,
                "format": metadata_entry.format,
                "md5_hash": metadata_entry.md5_hash,
                "created_at": metadata_entry.created_at,
                "updated_at": metadata_entry.updated_at,
                "title": metadata_entry.title,
                "author": metadata_entry.author,
                "subject": metadata_entry.subject,
                "keywords": metadata_entry.keywords,
                "creator": metadata_entry.creator,
                "producer": metadata_entry.producer,
                "creationdate": metadata_entry.creationdate,
                "moddate": metadata_entry.moddate,
                "last_modified_by": metadata_entry.last_modified_by,
                "category": metadata_entry.category,
                "content_status": metadata_entry.content_status,
                "revision": metadata_entry.revision,
                "trapped": metadata_entry.trapped,
                "encryption": metadata_entry.encryption,
                "page_count": metadata_entry.page_count,
                "is_encrypted": metadata_entry.is_encrypted,
                "fonts": metadata_entry.fonts,
                "page_rotation": metadata_entry.page_rotation,
                "pdfminer_info": metadata_entry.pdfminer_info,
                "metadata_stream": metadata_entry.metadata_stream,
                "tagged": metadata_entry.tagged,
                "userproperties": metadata_entry.userproperties,
                "suspects": metadata_entry.suspects,
                "form": metadata_entry.form,
                "javascript": metadata_entry.javascript,
                "pages": metadata_entry.pages,
                "encrypted": metadata_entry.encrypted,
                "page_size": metadata_entry.page_size,
                "optimized": metadata_entry.optimized,
                "pdf_version": metadata_entry.pdf_version,
                "word_count": metadata_entry.word_count,
            },
            status=status.HTTP_200_OK,
        )
        