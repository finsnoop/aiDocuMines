import logging
import uuid

from django.shortcuts import get_object_or_404
from django.db import transaction
from django.db.models import Count, Q, F
from django.utils import timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication

from core.models import BulkJob, BulkJobFileResult, File
from core.bulk_registry import get_bulk_handler, list_bulk_handlers
from core.service_catalog import ALL_SERVICES
from document_operations.models import Folder, FileFolderLink

logger = logging.getLogger(__name__)


def get_user_from_client_id(client_id):
    from oauth2_provider.models import Application
    try:
        application = Application.objects.get(client_id=client_id)
        return application.user
    except Application.DoesNotExist:
        return None


client_id_param = openapi.Parameter(
    "X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True,
    description="Client ID provided at signup"
)


class BulkServiceCatalogView(APIView):
    """List all available bulk processing services."""

    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="List all available bulk processing services",
        tags=["Bulk Processing"],
        manual_parameters=[client_id_param],
        responses={200: "List of services"}
    )
    def get(self, request):
        handlers = {h["job_type"] for h in list_bulk_handlers()}
        enriched = []
        for svc in ALL_SERVICES:
            entry = dict(svc)
            hjt = svc.get("handler_job_type")
            entry["has_handler"] = hjt in handlers if hjt else False
            enriched.append(entry)
        return Response({
            "count": len(enriched),
            "services": enriched,
        })


class BulkJobSubmitView(APIView):
    """Submit a new bulk processing job."""

    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Submit a new bulk processing job for multiple files",
        tags=["Bulk Processing"],
        manual_parameters=[client_id_param],
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=["job_type", "project_id", "service_id"],
            properties={
                "job_type": openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description="Type of bulk job (see /bulk/catalog/)"
                ),
                "file_ids": openapi.Schema(
                    type=openapi.TYPE_ARRAY,
                    items=openapi.Schema(type=openapi.TYPE_INTEGER),
                    description="List of file IDs to process"
                ),
                "folder_id": openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description="UUID of folder to process all files from"
                ),
                "project_id": openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description="Project ID"
                ),
                "service_id": openapi.Schema(
                    type=openapi.TYPE_STRING,
                    description="Service ID"
                ),
                "parameters": openapi.Schema(
                    type=openapi.TYPE_OBJECT,
                    description="Service-specific parameters"
                ),
            }
        ),
        responses={201: "Job created", 400: "Bad Request"}
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        access_token = request.headers.get("Authorization", "").split("Bearer ")[-1]
        if not access_token:
            return Response({"error": "Authorization token missing"}, status=401)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=401)

        raw_job_type = request.data.get("job_type")
        project_id = request.data.get("project_id")
        service_id = request.data.get("service_id")
        file_ids = request.data.get("file_ids", [])
        folder_id = request.data.get("folder_id")
        parameters = request.data.get("parameters", {})

        logger.debug("Bulk submit request: job_type=%s, project_id=%s, folder_id=%s, file_ids=%s",
                     raw_job_type, project_id, folder_id, file_ids)

        if not raw_job_type or not project_id or not service_id:
            return Response(
                {"error": "job_type, project_id, and service_id are required"},
                status=400
            )

        # Look up the service catalog entry and resolve handler_job_type
        svc_entry = None
        for svc in ALL_SERVICES:
            if svc["service_type"] == raw_job_type:
                svc_entry = svc
                break

        if not svc_entry:
            return Response(
                {"error": f"Unknown service_type: {raw_job_type}. See /bulk/catalog/ for available services."},
                status=400
            )

        job_type = svc_entry.get("handler_job_type")
        if not job_type:
            return Response(
                {"error": f"Service '{raw_job_type}' has no bulk handler configured."},
                status=400
            )

        handler = get_bulk_handler(job_type)
        if not handler:
            return Response(
                {"error": f"No handler registered for '{job_type}'. See /bulk/catalog/ for available services."},
                status=400
            )

        accepted_extensions = svc_entry.get("accepted_extensions")

        resolved_file_ids = set()

        if file_ids:
            resolved_file_ids.update(file_ids)

        if folder_id:
            try:
                folder_uuid = uuid.UUID(str(folder_id))
                folder = get_object_or_404(Folder, pk=folder_uuid, user=user)
                q = FileFolderLink.objects.filter(folder=folder, is_trashed=False)
                total_in_folder = q.count()
                if accepted_extensions:
                    q = q.filter(file__extension__in=accepted_extensions)
                    matching = q.count()
                    logger.debug("Folder %s: %d total files, %d matching extensions %s",
                                 folder_id, total_in_folder, matching, accepted_extensions)
                resolved_file_ids.update(q.values_list("file_id", flat=True))
                logger.debug("Resolved %d file_ids from folder %s", len(resolved_file_ids), folder_id)
            except (ValueError, Folder.DoesNotExist):
                logger.warning("Folder lookup failed for folder_id=%s", folder_id)
                return Response({"error": f"Folder {folder_id} not found"}, status=404)

        if not resolved_file_ids:
            return Response(
                {"error": "No files selected. Provide file_ids or folder_id."},
                status=400
            )

        existing_files = set(
            File.objects.filter(
                id__in=list(resolved_file_ids),
                user=user
            ).values_list("id", flat=True)
        )

        # Log how many files were skipped due to extension filtering
        if folder_id and accepted_extensions:
            unfiltered_count = FileFolderLink.objects.filter(
                folder=folder, is_trashed=False
            ).count()
            if unfiltered_count > len(resolved_file_ids):
                logger.info(
                    f"Extension filter ({accepted_extensions}) skipped "
                    f"{unfiltered_count - len(resolved_file_ids)} files in folder {folder_id}"
                )

        if not existing_files:
            return Response(
                {"error": "No valid files found for the given IDs"},
                status=400
            )

        missing = resolved_file_ids - existing_files
        if missing:
            logger.warning(f"Files not found or not owned by user: {missing}")

        file_ids_list = sorted(existing_files)

        with transaction.atomic():
            job = BulkJob.objects.create(
                user=user,
                job_type=job_type,
                status="Pending",
                total_files=len(file_ids_list),
                project_id=project_id,
                service_id=service_id,
                folder_id=folder_id if folder_id else None,
                input_parameters=parameters,
            )

            results = [
                BulkJobFileResult(bulk_job=job, file_id=fid, status="Pending")
                for fid in file_ids_list
            ]
            BulkJobFileResult.objects.bulk_create(results, batch_size=500)

        from core.tasks_bulk import execute_bulk_job
        execute_bulk_job.delay(str(job.id))

        return Response({
            "job_id": str(job.id),
            "job_type": job_type,
            "status": "Queued",
            "total_files": len(file_ids_list),
            "message": f"Bulk job queued with {len(file_ids_list)} files",
        }, status=201)


class BulkJobStatusView(APIView):
    """Get the status of a bulk processing job."""

    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Get bulk job status and progress",
        tags=["Bulk Processing"],
        manual_parameters=[
            client_id_param,
            openapi.Parameter("job_id", openapi.IN_PATH, type=openapi.TYPE_STRING, required=True),
        ],
        responses={200: "Job status"}
    )
    def get(self, request, job_id):
        client_id = request.headers.get("X-Client-ID")
        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=401)

        job = get_object_or_404(BulkJob, id=job_id, user=user)

        result_counts = BulkJobFileResult.objects.filter(bulk_job=job).aggregate(
            total=Count("id"),
            completed=Count("id", filter=Q(status="Completed")),
            failed=Count("id", filter=Q(status="Failed")),
            processing=Count("id", filter=Q(status="Processing")),
            pending=Count("id", filter=Q(status="Pending")),
            skipped=Count("id", filter=Q(status="Skipped")),
        )

        return Response({
            "job_id": str(job.id),
            "job_type": job.job_type,
            "status": job.status,
            "total_files": job.total_files,
            "progress": {
                "completed": result_counts["completed"],
                "failed": result_counts["failed"],
                "processing": result_counts["processing"],
                "pending": result_counts["pending"],
                "skipped": result_counts["skipped"],
            },
            "result_summary": job.result_summary,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
        })


class BulkJobResultsView(APIView):
    """Get paginated results for a bulk job."""

    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Get paginated file results for a bulk job",
        tags=["Bulk Processing"],
        manual_parameters=[
            client_id_param,
            openapi.Parameter("job_id", openapi.IN_PATH, type=openapi.TYPE_STRING, required=True),
            openapi.Parameter("status", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False,
                              description="Filter by status: Completed, Failed, Pending, Processing"),
            openapi.Parameter("page", openapi.IN_QUERY, type=openapi.TYPE_INTEGER, default=1),
            openapi.Parameter("page_size", openapi.IN_QUERY, type=openapi.TYPE_INTEGER, default=100),
        ],
        responses={200: "Paginated results"}
    )
    def get(self, request, job_id):
        client_id = request.headers.get("X-Client-ID")
        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=401)

        job = get_object_or_404(BulkJob, id=job_id, user=user)
        qs = BulkJobFileResult.objects.filter(bulk_job=job).select_related("file")

        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        page = int(request.query_params.get("page", 1))
        page_size = min(int(request.query_params.get("page_size", 100)), 500)
        offset = (page - 1) * page_size
        total = qs.count()

        results = qs[offset:offset + page_size]

        return Response({
            "job_id": str(job.id),
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
            "results": [
                {
                    "file_id": r.file_id,
                    "filename": r.file.filename if r.file else None,
                    "status": r.status,
                    "result_data": r.result_data,
                    "error_message": r.error_message,
                    "task_id": r.task_id,
                    "started_at": r.started_at,
                    "completed_at": r.completed_at,
                }
                for r in results
            ],
        })


class BulkJobListView(APIView):
    """List all bulk jobs for the authenticated user."""

    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="List all bulk jobs for the current user",
        tags=["Bulk Processing"],
        manual_parameters=[
            client_id_param,
            openapi.Parameter("status", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter("limit", openapi.IN_QUERY, type=openapi.TYPE_INTEGER, default=50),
        ],
        responses={200: "List of jobs"}
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=401)

        qs = BulkJob.objects.filter(user=user)

        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)

        limit = min(int(request.query_params.get("limit", 50)), 200)
        jobs = qs.order_by("-created_at")[:limit]

        return Response({
            "count": jobs.count(),
            "jobs": [
                {
                    "job_id": str(j.id),
                    "job_type": j.job_type,
                    "status": j.status,
                    "total_files": j.total_files,
                    "processed_files": j.processed_files,
                    "failed_files": j.failed_files,
                    "result_summary": j.result_summary,
                    "created_at": j.created_at,
                    "completed_at": j.completed_at,
                }
                for j in jobs
            ],
        })


class BulkJobCancelView(APIView):
    """Cancel a running bulk job."""

    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    def post(self, request, job_id):
        client_id = request.headers.get("X-Client-ID")
        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=401)

        job = get_object_or_404(BulkJob, id=job_id, user=user)

        if job.status not in ("Pending", "Queued", "Processing"):
            return Response({
                "error": f"Cannot cancel job in status '{job.status}'"
            }, status=400)

        from core.tasks_bulk import cancel_bulk_job
        cancel_bulk_job.delay(str(job.id))

        return Response({
            "job_id": str(job.id),
            "message": "Job cancellation requested",
        })


class BulkJobRetryView(APIView):
    """Retry failed files in a bulk job."""

    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    def post(self, request, job_id):
        client_id = request.headers.get("X-Client-ID")
        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=401)

        job = get_object_or_404(BulkJob, id=job_id, user=user)

        failed = BulkJobFileResult.objects.filter(bulk_job=job, status="Failed")
        skipped = BulkJobFileResult.objects.filter(bulk_job=job, status="Skipped")
        retry_count = failed.count() + skipped.count()

        if retry_count == 0:
            return Response({"message": "No failed or skipped files to retry"})

        failed.update(status="Pending", error_message=None, task_id=None,
                      completed_at=None, started_at=None)
        skipped.update(status="Pending", error_message=None, task_id=None,
                       completed_at=None, started_at=None)

        job.status = "Pending"
        job.save(update_fields=["status"])

        from core.tasks_bulk import execute_bulk_job
        execute_bulk_job.delay(str(job.id))

        return Response({
            "job_id": str(job.id),
            "message": f"Retrying {retry_count} files",
            "retry_count": retry_count,
        })
