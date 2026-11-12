from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework import status
from django.conf import settings
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
import os
import uuid
from datetime import datetime
from .models import Run, File, Storage, EndpointResponseTable, Metadata
from .tasks import process_metadata
from django.shortcuts import get_object_or_404
from oauth2_provider.models import Application
from oauth2_provider.contrib.rest_framework import OAuth2Authentication, TokenHasReadWriteScope
from .utils import save_uploaded_file
from django.http import FileResponse
import logging
from django.db.utils import IntegrityError
from django.http import JsonResponse

from core.models import File
from grid_documents_interrogation.models import Topic
from django.shortcuts import get_object_or_404
from rest_framework import status, permissions
from rest_framework.permissions import IsAuthenticated
from custom_authentication.permissions import IsClientOrAdminOrSuperUser
from document_operations.models import Folder, FileFolderLink
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
import mimetypes
from document_operations.utils import register_file_folder_link

from core.tasks import extract_document_text_task
from document_search.tasks import index_file  # at the top of views.py
from django.db import transaction

from platform_data_insights.tasks import generate_insights_for_user
from platform_data_insights.models import UserInsights

from core.tasks import process_bulk_metadata

import mimetypes

from document_anonymizer.models import AnonymizationRun
from document_anonymizer.tasks import anonymize_document_task, compute_risk_score_task
from core.access import get_file_for_user, get_scoped_or_404, get_run_for_user, scope_by_file, scope_by_owner


logger = logging.getLogger(__name__)

# ✅ Define Swagger parameters
client_id_param = openapi.Parameter(
    "X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True, description="Client ID provided at signup"
)
project_id_param = openapi.Parameter(
    "project_id", openapi.IN_FORM, type=openapi.TYPE_STRING, required=True, description="Project ID"
)
service_id_param = openapi.Parameter(
    "service_id", openapi.IN_FORM, type=openapi.TYPE_STRING, required=True, description="Service ID"
)
file_param = openapi.Parameter(
    "file", openapi.IN_FORM, type=openapi.TYPE_FILE, required=True, description="File(s) to upload"
)
file_id_param = openapi.Parameter(
    "file_id", openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True, description="Unique File ID"
)


def get_user_from_client_id(client_id):
    """Retrieve the User associated with a given client_id from OAuth2 Application."""
    try:
        application = Application.objects.get(client_id=client_id)
        return application.user
    except Application.DoesNotExist:
        return None

def health_check(request):
    return JsonResponse({"status": "ok"}, status=200)



# views.py  ────────────────────────────────────────────────────────────────────
@method_decorator(csrf_exempt, name="dispatch")
class FileUploadView(APIView):
    """
    Upload files securely using OAuth2 authentication.

    • Blocks accidental re-uploads by the SAME user (md5 + user scope)
    • Re-uses storage when a DIFFERENT user uploads the same file
    • Allows intentional clones when   clone_file=true   is sent
    """
    parser_classes = [MultiPartParser, FormParser]
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Upload multiple files securely using OAuth2 authentication. "
                              "Set clone_file=true to create an intentional copy.",
        tags=["File Upload"],
        manual_parameters=[client_id_param, project_id_param, service_id_param, file_param],
        responses={201: "Upload Successful", 400: "Bad Request", 403: "Forbidden"},
    )
    def post(self, request, *args, **kwargs):
        # ── Auth & basic params ───────────────────────────────────────────────
        client_id = request.headers.get("X-Client-ID")
        access_token = request.headers.get("Authorization", "").split("Bearer ")[-1]

        if not access_token:
            return Response({"error": "Authorization token missing"}, status=401)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=401)

        project_id = request.data.get("project_id")
        service_id = request.data.get("service_id")
        files      = request.FILES.getlist("file")
        clone_file = request.data.get("clone_file", "false").lower() == "true"

        #if not all([project_id, service_id]) or not files:
        #    return Response({"error": "Missing required fields"}, status=400)

        if not (project_id and project_id.strip()) or not (service_id and service_id.strip()) or not files:
            return Response({"error": "Missing required fields"}, status=400)

        # ── Per-upload run & path scaffold ───────────────────────────────────
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        run       = Run.objects.create(user=user, status="Pending")
        #base_dir  = os.path.join(
        #    settings.MEDIA_ROOT, "uploads", str(user.id), timestamp,
        #    client_id, project_id, service_id, str(run.run_id)
        #)
        # base_dir  = os.path.join(
        #     settings.MEDIA_ROOT, "uploads", client_id, str(user.id), project_id, service_id, timestamp, str(run.run_id))
        # os.makedirs(base_dir, exist_ok=True)
        
        base_dir  = os.path.join(
                settings.MEDIA_ROOT, "uploads", client_id, str(user.id), project_id, service_id, str(timestamp)[:8])
        os.makedirs(base_dir, exist_ok=True)

        file_payload = []

        # ── Iterate through each uploaded file ───────────────────────────────
        for uploaded in files:
            temp_dir = os.path.join(base_dir, "temp")
            os.makedirs(temp_dir, exist_ok=True)

            meta = save_uploaded_file(uploaded, temp_dir)     # md5, size, etc.

            # ---- 1️⃣ same-user duplicate check -----------------------------
            dup_self = File.objects.filter(user=user, md5_hash=meta["md5_hash"]).first()
            if dup_self:
                if not clone_file:
                    os.remove(meta["file_path"])
                    return Response({
                        "message"   : "Duplicate file already exists under your account.",
                        "file_id"   : dup_self.id,
                        "filename"  : dup_self.filename,
                        "md5_hash"  : dup_self.md5_hash,
                        "project_id": dup_self.project_id,
                        "service_id": dup_self.service_id,
                        "filepath"  : dup_self.filepath,
                        "extension" : dup_self.extension
                    }, status=200)

                # clone requested
                logger.info("📄 Cloning file for user %s : %s", user.id, meta["filename"])
                os.remove(meta["file_path"])  # cleanup temp
                clone = File.objects.create(
                    run=run,
                    storage=dup_self.storage,
                    filename=dup_self.filename,
                    filepath=dup_self.filepath,
                    file_size=dup_self.file_size,
                    file_type=dup_self.file_type,
                    md5_hash=dup_self.md5_hash,
                    user=user,
                    project_id=project_id,
                    service_id=service_id,
                    origin_file=dup_self,
                    extension=dup_self.extension,
                )
                register_file_folder_link(clone)
                # ✅ REMOVED: folder_name logic was creating duplicate root-level folders
                # register_file_folder_link already creates the correct folder hierarchy

                file_payload.append(_resp(clone, "File cloned for reuse."))
                continue  # next upload

            # ---- 2️⃣ cross-user duplicate check ----------------------------
            dup_other = File.objects.exclude(user=user).filter(md5_hash=meta["md5_hash"]).first()
            if dup_other:
                logger.info("🔗 Re-using storage from user %s for user %s", dup_other.user_id, user.id)
                os.remove(meta["file_path"])
                reused = File.objects.create(
                    run=run,
                    storage=dup_other.storage,
                    filename=dup_other.filename,
                    filepath=dup_other.filepath,
                    file_size=dup_other.file_size,
                    file_type=dup_other.file_type,
                    md5_hash=dup_other.md5_hash,
                    user=user,
                    project_id=project_id,
                    service_id=service_id,
                    extension=dup_other.extension
                )
                register_file_folder_link(reused)
                # ✅ REMOVED: folder_name logic was creating duplicate root-level folders
                # register_file_folder_link already creates the correct folder hierarchy

                file_payload.append(_resp(reused, "Duplicate file reused from another user."))
                continue

            # ---- 3️⃣ brand-new upload --------------------------------------
            # If folder_name is provided, include it in the path so register_file_folder_link
            # creates the correct folder hierarchy (e.g., 20260102/create-deal-workspace/file.html)
            folder_name = request.data.get("folder_name") or request.query_params.get("folder_name")
            if folder_name:
                final_dir = os.path.join(base_dir, folder_name)
            else:
                final_dir = base_dir
            os.makedirs(final_dir, exist_ok=True)
            final_path = os.path.join(final_dir, str(timestamp)[8:]+"_"+meta["filename"])
            os.rename(meta["file_path"], final_path)

            storage = Storage.objects.create(
                user=user,
                content_type=ContentType.objects.get_for_model(run),
                upload_storage_location=final_path
            )

            _, guessed_ext = mimetypes.guess_type(meta["filename"])
            if guessed_ext:
                extension = guessed_ext.split("/")[-1].lower()
            else:
                extension = os.path.splitext(meta["filename"])[-1].replace('.', '').lower() or "unknown"

            fresh = File.objects.create(
                run=run,
                storage=storage,
                filename=meta["filename"],
                filepath=final_path,
                file_size=meta["file_size"],
                # file_type=meta["file_type"],
                file_type = meta.get("file_type") or mimetypes.guess_type(meta["filename"])[0] or "application/octet-stream",
                md5_hash=meta["md5_hash"],
                user=user,
                project_id=project_id,
                service_id=service_id,
                extension=extension
            )
            # register_file_folder_link creates folder hierarchy based on filepath
            # When folder_name is provided, the file is stored in:
            # uploads/{client}/{user}/{project}/{service}/{YYYYMMDD}/{folder_name}/{file}
            # This creates folders like: 20260102/create-deal-workspace/Report.html
            register_file_folder_link(fresh)

            file_payload.append(_resp(fresh, "File uploaded successfully."))


            def trigger_indexing():
                try:
                    from core.models import EndpointResponseTable

                    endpoint_response = EndpointResponseTable.objects.create(
                        run=run,
                        client=user,  # ✅ correct field name in model
                        endpoint_name="/api/v1/document-search/index/",
                        response_data={"auto_trigger": True},  # ✅ corresponds to JSONField
                        status="Pending"  # ✅ must match one of the allowed choices
                    )
                    logger.info("✅ Created EndpointResponseTable entry with ID: %s for file_id=%s", endpoint_response.id, fresh.id)

                    index_file.apply_async((fresh.id,), {"force": True}, task_id=str(endpoint_response.id))
                    logger.info("🚀 Indexing task successfully queued for file_id=%s", fresh.id)

                except Exception as e:
                    logger.exception("❌ Failed to trigger indexing for file_id=%s. Error: %s", fresh.id, str(e))


            def trigger_anonymization():
                try:
                    # Decide anonymization file_type for your pipeline
                    # Use "structured" if you want JSON blocks; fallback to "plain"
                    file_type = "structured" if fresh.file_type.lower() in [
                        "application/pdf",
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    ] else "plain"

                    # Create a run record to track this anonymization
                    anon_run = AnonymizationRun.objects.create(
                        project_id=project_id,
                        service_id=service_id,
                        client=getattr(user, 'client', None),
                        client_name=user.username or user.email,
                        status="Processing",
                        anonymization_type="Presidio"  # or "Presidio-Spacy" to match your choice
                    )

                    # Queue anonymization (idempotence handled inside the task if you prefer)
                    anonymize_document_task.apply_async((fresh.id, file_type, str(anon_run.id)))

                    # If your anonymize task doesn’t already compute risk, also queue this:
                    compute_risk_score_task.apply_async((fresh.id,), countdown=0)

                    # (Optional) log to EndpointResponseTable (like you do for indexing)
                    EndpointResponseTable.objects.create(
                        run=run,
                        client=user,
                        endpoint_name="/api/v1/document-anonymizer/submit-anonymization/",
                        response_data={"auto_trigger": True, "file_id": fresh.id, "file_type": file_type},
                        status="Pending"
                    )
                    logger.info("🔐 Auto-anonymization queued for file_id=%s (type=%s)", fresh.id, file_type)
                except Exception:
                    logger.exception("❌ Failed to queue anonymization for file_id=%s", fresh.id)


            if fresh.file_type.lower() not in ["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]:
                logger.info("⏭️   Skipping indexing for unsupported type: %s", fresh.file_type)
            elif not getattr(getattr(user, "settings", None), "auto_index_enabled", True):
                logger.info("⚙️ Auto-indexing disabled for user %s", user.id)
            elif File.objects.filter(user=fresh.user, md5_hash=fresh.md5_hash, vector_chunks__isnull=False).exclude(id=fresh.id).exists():
                logger.info("♻️ Skipping indexing – vector chunks already exist for file with same MD5: %s", fresh.md5_hash)
            else:
                logger.info("🧠 Queueing indexing to happen after transaction commit")
                transaction.on_commit(trigger_indexing)

            # NEW:
            transaction.on_commit(lambda: extract_document_text_task.delay(fresh.id))
            
            # queue after transaction commits (same pattern you use for indexing)
            transaction.on_commit(trigger_anonymization)

            # Delete any old cached insights
            UserInsights.objects.filter(user=request.user).delete()

            # Launch async insights regeneration
            generate_insights_for_user.delay(request.user.id)

            process_bulk_metadata.delay(str(run.run_id))

        return Response({"run_id": str(run.run_id), "files": file_payload}, status=201)


# ── helper utilities (keep near the view or in utils.py) ─────────────────────
'''
def _link_to_folder(file_obj, user, project_id, service_id):
    folder, _ = Folder.objects.get_or_create(
        name=project_id,
        user=user,
        project_id=project_id,
        service_id=service_id,
        defaults={"created_at": timezone.now()},
    )
    FileFolderLink.objects.get_or_create(file=file_obj, folder=folder)
'''

def _link_to_folder(file_obj, user, project_id, service_id):
    from document_operations.utils import get_or_create_folder_tree, link_file_to_folder

    # Build relative path from final file location
    try:
        relative_path = file_obj.filepath.split(f"{project_id}/{service_id}/", 1)[-1]
        folder_parts = os.path.dirname(relative_path).split("/")
        leaf_folder = get_or_create_folder_tree(folder_parts, user=user, project_id=project_id, service_id=service_id)
        link_file_to_folder(file_obj, leaf_folder)
    except Exception as e:
        logger.warning(f"Failed to link file {file_obj.id} to folder tree: {e}")


def _resp(f, message):
    return {
        "file_id"  : f.id,
        "filename" : f.filename,
        "file_size": f.file_size,
        "mime_type": f.file_type,
        "message"  : message,
    }


class UniversalTaskStatusView(APIView):
    """
    Fetch the latest status for a given run_id from EndpointResponseTable.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Retrieve the status of an asynchronous task using run_id.",
        tags=["Task Status"],
        manual_parameters=[
            client_id_param,
            openapi.Parameter("run_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Unique Run ID to track processing"),
        ],
        responses={200: "Success", 404: "Not Found"},
    )
    def get(self, request):
        """
        Retrieve the task status using run_id.
        """
        client_id = request.headers.get("X-Client-ID")
        access_token_string = request.headers.get("Authorization", "").split("Bearer ")[-1]
        run_id = request.query_params.get("run_id")

        # ✅ Enforce OAuth2 Authentication
        if not access_token_string:
            return Response({"error": "Authorization token missing"}, status=status.HTTP_401_UNAUTHORIZED)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        if not run_id:
            return Response({"error": "Missing run_id parameter"}, status=status.HTTP_400_BAD_REQUEST)

        # ✅ Fetch task status
        response_entry = EndpointResponseTable.objects.filter(run__run_id=run_id).order_by("-created_at").first()

        if not response_entry:
            return Response({"error": "No status found for the provided run_id."}, status=status.HTTP_404_NOT_FOUND)

        return Response(
            {
                "run_id": run_id,
                "status": response_entry.status,
                "endpoint": response_entry.endpoint_name,
                "response_data": response_entry.response_data,
            },
            status=status.HTTP_200_OK if response_entry.status == "Completed" else status.HTTP_202_ACCEPTED,
        )

@method_decorator(csrf_exempt, name="dispatch")
class MetadataView(APIView):
    """
    ✅ Triggers metadata extraction for a given file.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Trigger metadata extraction for a specific file.",
        tags=["Core Application: Metadata Extraction"],
        manual_parameters=[client_id_param, file_id_param],
        responses={202: "Processing started", 400: "Bad Request", 404: "Not Found"},
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        file_id = request.query_params.get("file_id")

        # ✅ Fetch file instance
        file_instance = get_file_for_user(request.user, file_id)

        # ✅ Generate a new run_id for the metadata extraction process
        run_id = str(uuid.uuid4())
        run = Run.objects.create(run_id=run_id, user=file_instance.user, status="Processing")

        # ✅ Trigger metadata extraction task
        from .tasks import process_metadata
        process_metadata.delay(file_instance.id, run.run_id)

        return Response(
            {
                "message": "Metadata extraction started.",
                "run_id": run_id,
                "file_id": file_instance.id,
            },
            status=status.HTTP_202_ACCEPTED,
        )



class FileDownloadView(APIView):
    """
    ✅ Download a file using file_id, dynamically supporting all common formats.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Download a file using file_id. Supports multiple file types.",
        tags=["Core Application: File Download"],
        manual_parameters=[client_id_param, file_id_param],
        responses={200: "Success", 404: "File Not Found"},
    )
    def get(self, request):
        file_id = request.query_params.get("file_id")
        if not file_id:
            return Response({"error": "Missing file_id"}, status=400)

        try:
            file_obj = get_file_for_user(request.user, file_id)
        except File.DoesNotExist:
            return Response({"error": "File not found"}, status=404)

        real_path = file_obj.filepath.replace("/app/", "")  # Adjust path mapping if needed

        if not os.path.exists(real_path):
            return Response({"error": "File not found on disk"}, status=404)

        # Try to guess the content type from the filename
        content_type, _ = mimetypes.guess_type(real_path)
        if not content_type:
            content_type = "application/octet-stream"  # Fallback for unknown types

        filename = os.path.basename(real_path)

        # Serve inline for renderable types, attachment only when ?download=true
        is_download = request.query_params.get("download", "").lower() == "true"

        try:
            return FileResponse(
                open(real_path, "rb"),
                as_attachment=is_download,
                filename=filename,
                content_type=content_type
            )
        except Exception as e:
            return Response({"error": f"Failed to read file: {str(e)}"}, status=500)


'''
class FileDownloadView(APIView):
    """
    ✅ Download a file using file_id
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Download a file using file_id.",
        tags=["Core Application: File Download"],
        manual_parameters=[client_id_param, file_id_param],
        responses={200: "Success", 404: "File Not Found"},
    )

    def get(self, request):
        file_id = request.query_params.get("file_id")
        if not file_id:
            return Response({"error": "Missing file_id"}, status=400)

        try:
            file = File.objects.get(id=file_id)
        except File.DoesNotExist:
            return Response({"error": "File not found"}, status=404)

        real_path = file.filepath.replace("/app/", "")  # Map container path to host
        if not os.path.exists(real_path):
            return Response({"error": "File not found on disk"}, status=404)

        return FileResponse(
            open(real_path, "rb"),
            as_attachment=False,
            filename=os.path.basename(real_path),
            content_type="application/pdf"
        )


    '''


@method_decorator(csrf_exempt, name="dispatch")
class BulkFolderUploadView(APIView):
    """
    Upload a folder of documents (recursive) and preserve subdirectory structure.
    """
    parser_classes = [MultiPartParser, FormParser]
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Upload a folder with multiple documents. Subdirectory structure is preserved.",
        tags=["Folder Upload"],
        manual_parameters=[client_id_param, project_id_param, service_id_param, file_param],
        responses={201: "Bulk Upload Successful", 400: "Bad Request", 403: "Forbidden"},
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        access_token_string = request.headers.get("Authorization", "").split("Bearer ")[-1]

        if not access_token_string:
            return Response({"error": "Authorization token missing"}, status=status.HTTP_401_UNAUTHORIZED)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        project_id = request.data.get("project_id")
        service_id = request.data.get("service_id")
        files = request.FILES.getlist("file")

        #if not all([client_id, project_id, service_id]) or not files:
        #    return Response({"error": "Missing required fields"}, status=status.HTTP_400_BAD_REQUEST)

        if not (project_id and project_id.strip()) or not (service_id and service_id.strip()) or not files:
            return Response({"error": "Missing required fields"}, status=400)

        user_id = str(user.id)
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        run_id = str(uuid.uuid4())
        run = Run.objects.create(run_id=run_id, user=user, status="Pending")

        upload_dir_base = os.path.join(settings.MEDIA_ROOT, "uploads", user_id, timestamp, client_id, project_id, service_id, run_id)
        os.makedirs(upload_dir_base, exist_ok=True)

        uploaded_files_data = []

        for f in files:
            relative_path = f.name  # `webkitRelativePath` from frontend
            target_path = os.path.join(upload_dir_base, relative_path)
            os.makedirs(os.path.dirname(target_path), exist_ok=True)

            file_details = save_uploaded_file(f, os.path.dirname(target_path), custom_filename=os.path.basename(target_path))

            try:
                storage = Storage.objects.create(user=user, content_type=ContentType.objects.get_for_model(run.__class__), upload_storage_location=file_details["file_path"])

                file_instance = File.objects.create(
                    run=run,
                    storage=storage,
                    filename=file_details["filename"],
                    filepath=file_details["file_path"],
                    file_size=file_details["file_size"],
                    file_type=file_details["file_type"],
                    md5_hash=file_details["md5_hash"],
                    user=user,
                    project_id=project_id,
                    service_id=service_id,
                )
                # Create folder link based on file path structure
                # This creates the proper nested folder hierarchy and links the file
                register_file_folder_link(file_instance)

                uploaded_files_data.append({
                    "file_id": file_instance.id,
                    "filename": file_details["filename"],
                    "relative_path": relative_path,
                    "file_size": file_details["file_size"],
                    "mime_type": file_details["file_type"],
                })


            except IntegrityError as e:
                if 'uniq_file_md5_per_user' in str(e) or 'core_file_md5_hash_key' in str(e):
                    # Only ever reveal the caller's OWN duplicate, never another tenant's file.
                    existing = File.objects.filter(user=request.user, md5_hash=file_details["md5_hash"]).first()
                    return Response({
                        "error": "Duplicate file detected.",
                        "filename": file_details["filename"],
                        "existing_file_id": existing.id if existing else None,
                    }, status=status.HTTP_400_BAD_REQUEST)
                else:
                    logger.exception("Folder upload integrity error")
                    return Response({"error": "Could not save the uploaded file."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({
            "message": "Folder upload successful.",
            "run_id": run_id,
            "files": uploaded_files_data,
        }, status=status.HTTP_201_CREATED)



class AssociateTopicToFileView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def patch(self, request, file_id):
        topic_id = request.data.get("topic_id")
        if not topic_id:
            return Response({"error": "Missing topic_id"}, status=status.HTTP_400_BAD_REQUEST)

        file = get_object_or_404(File, pk=file_id, user=request.user)
        topic = get_object_or_404(Topic, pk=topic_id, user=request.user)

        file.topic = topic
        file.save()

        return Response({"message": f"File {file.id} successfully linked to topic {topic.id}"}, status=status.HTTP_200_OK) 






class FileInsightView(APIView):
    """
    Returns full insight for a given file_id including run, project, and metadata links.
    Supports optional report generation via POST.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Retrieve insight for a specific file including metadata and associated run.",
        tags=["Client Intelligence"],
        manual_parameters=[client_id_param, file_id_param],
        responses={200: "Success", 404: "File Not Found"},
    )
    def get(self, request):
        file_id = request.query_params.get("file_id")
        if not file_id:
            return Response({"error": "Missing file_id"}, status=400)

        file = get_file_for_user(request.user, file_id)
        metadata = file.metadata.first()
        run = file.run

        return Response({
            "file": {
                "file_id": file.id,
                "filename": file.filename,
                "file_type": file.file_type,
                "file_size": file.file_size,
                "status": file.status,
                "project_id": file.project_id,
                "service_id": file.service_id,
            },
            "run": {
                "run_id": str(run.run_id),
                "status": run.status,
                "created_at": str(run.created_at),
                "cost": float(run.cost),
            },
            "metadata": {
                "title": metadata.title if metadata else None,
                "page_count": metadata.page_count if metadata else None,
                "author": metadata.author if metadata else None,
                "keywords": metadata.keywords if metadata else None,
            }
        })

    @swagger_auto_schema(
        operation_description="Generate and register a file insight report.",
        tags=["Client Intelligence"],
        manual_parameters=[
            client_id_param,
            file_id_param,
            openapi.Parameter("project_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True),
            openapi.Parameter("service_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True),
        ],
        responses={200: "Success", 404: "File Not Found"},
    )
    def post(self, request):
        """Generate a file insight report and register it in the file tree."""
        import time
        from core.utils import generate_and_register_service_report

        start_time = time.time()
        file_id = request.query_params.get("file_id")
        project_id = request.query_params.get("project_id")
        service_id = request.query_params.get("service_id")

        if not file_id:
            return Response({"error": "Missing file_id"}, status=400)
        if not project_id or not service_id:
            return Response({"error": "Missing project_id or service_id"}, status=400)

        file = get_file_for_user(request.user, file_id)
        metadata = file.metadata.first()
        run = file.run

        response_data = {
            "file": {
                "file_id": file.id,
                "filename": file.filename,
                "file_type": file.file_type,
                "file_size": file.file_size,
                "status": file.status,
                "project_id": file.project_id,
                "service_id": file.service_id,
            },
            "run": {
                "run_id": str(run.run_id),
                "status": run.status,
                "created_at": str(run.created_at),
                "cost": float(run.cost),
            },
            "metadata": {
                "title": metadata.title if metadata else None,
                "page_count": metadata.page_count if metadata else None,
                "author": metadata.author if metadata else None,
                "keywords": metadata.keywords if metadata else None,
                "format": metadata.format if metadata else None,
                "creator": metadata.creator if metadata else None,
                "producer": metadata.producer if metadata else None,
            }
        }

        execution_time = time.time() - start_time

        try:
            report_info = generate_and_register_service_report(
                service_name="File Insight Analysis",
                service_id="ai-file-insight",
                vertical="AI Services",
                response_data=response_data,
                user=request.user,
                run=run,
                project_id=project_id,
                service_id_folder=service_id,
                folder_name="file-insight-results",
                execution_time_seconds=execution_time,
                input_files=[{"filename": file.filename, "file_id": file.id}],
                additional_metadata={
                    "file_type": file.file_type,
                    "page_count": metadata.page_count if metadata else 0
                }
            )
            response_data["report_file"] = report_info
            logger.info(f"✅ Generated file insight report: {report_info.get('filename')}")
        except Exception as report_error:
            logger.warning(f"Failed to generate report: {report_error}")
            response_data["report_error"] = str(report_error)

        return Response(response_data)



class RunSummaryView(APIView):
    """
    Summarize a processing run by run_id.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Get a summary of a run including all linked files.",
        tags=["Client Intelligence"],
        manual_parameters=[
            client_id_param,
            openapi.Parameter("run_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Run UUID"),
        ],
        responses={200: "Success", 404: "Not Found"},
    )
    def get(self, request):
        run_id = request.query_params.get("run_id")
        if not run_id:
            return Response({"error": "Missing run_id"}, status=400)

        run = get_object_or_404(scope_by_owner(request.user, Run.objects.all()), run_id=run_id)
        files = run.files.all()

        return Response({
            "run_id": str(run.run_id),
            "status": run.status,
            "created_at": run.created_at,
            "cost": float(run.cost),
            "file_count": files.count(),
            "files": [
                {
                    "file_id": f.id,
                    "filename": f.filename,
                    "file_type": f.file_type,
                    "file_size": f.file_size,
                    "project_id": f.project_id,
                    "service_id": f.service_id,
                }
                for f in files
            ]
        })




class ClientSummaryView(APIView):
    """
    Summarize total activity for the current OAuth2 user.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Returns a summary of the current user's files, runs, and storage usage.",
        tags=["Client Intelligence"],
        manual_parameters=[client_id_param],
        responses={200: "Success"},
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=401)

        files = File.objects.filter(user=user)
        total_bytes = sum(f.file_size for f in files)
        recent_files = files.order_by("-created_at")[:5]

        return Response({
            "total_runs": Run.objects.filter(user=user).count(),
            "total_files": files.count(),
            "total_storage_mb": round(total_bytes / (1024 * 1024), 2),
            "recent_files": [
                {"file_id": f.id, "filename": f.filename, "created_at": f.created_at}
                for f in recent_files
            ]
        })




class StorageLocationsView(APIView):
    """
    Show storage paths for a given file_id (upload and output).
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Returns upload and output paths for a file.",
        tags=["Client Intelligence"],
        manual_parameters=[client_id_param, file_id_param],
        responses={200: "Success", 404: "File Not Found"},
    )
    def get(self, request):
        file_id = request.query_params.get("file_id")
        if not file_id:
            return Response({"error": "Missing file_id"}, status=400)

        file = get_file_for_user(request.user, file_id)
        storage = file.storage

        return Response({
            "upload_path": storage.upload_storage_location if storage else None,
            "output_path": storage.output_storage_location if storage else None,
        })





class ProjectSummaryView(APIView):
    """
    Lists all files linked to a project_id for the current user.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Returns files belonging to a specific project.",
        tags=["Client Intelligence"],
        manual_parameters=[
            client_id_param,
            openapi.Parameter("project_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Project ID"),
        ],
        responses={200: "Success"},
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        project_id = request.query_params.get("project_id")
        user = get_user_from_client_id(client_id)

        if not project_id:
            return Response({"error": "Missing project_id"}, status=400)

        files = File.objects.filter(user=user, project_id=project_id)

        return Response({
            "project_id": project_id,
            "total_files": files.count(),
            "files": [
                {"file_id": f.id, "filename": f.filename, "file_type": f.file_type}
                for f in files
            ]
        })



class ServiceSummaryView(APIView):
    """
    Lists all files linked to a service_id for the current user.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Returns files linked to a specific service.",
        tags=["Client Intelligence"],
        manual_parameters=[
            client_id_param,
            openapi.Parameter("service_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Service ID"),
        ],
        responses={200: "Success"},
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        service_id = request.query_params.get("service_id")
        user = get_user_from_client_id(client_id)

        if not service_id:
            return Response({"error": "Missing service_id"}, status=400)

        files = File.objects.filter(user=user, service_id=service_id)

        return Response({
            "service_id": service_id,
            "total_files": files.count(),
            "files": [
                {"file_id": f.id, "filename": f.filename, "file_type": f.file_type}
                for f in files
            ]
        })




