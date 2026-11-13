import os
import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from django.contrib.auth import get_user_model
from oauth2_provider.models import Application
from core.models import File
from core.utils import get_logical_path
from document_versioning.models import DocumentVersion, VersionDiff, VersionRetentionPolicy, VersionManifest, VersionArchiveRecord
from document_versioning.tasks import create_version_task
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from document_versioning.utils import get_next_version_number, generate_diff, restore_from_archive


logger = logging.getLogger(__name__)

User = get_user_model()

client_id_param = openapi.Parameter(
    "X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True, description="Client ID for authentication"
)
client_secret_param = openapi.Parameter(
    "X-Client-Secret", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True, description="Client Secret for authentication"
)
file_id_param = openapi.Parameter(
    "file_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Unique File ID"
)
version_id_param = openapi.Parameter(
    "version_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Version ID"
)
from_version_param = openapi.Parameter(
    "from_version", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="From version ID"
)
to_version_param = openapi.Parameter(
    "to_version", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="To version ID"
)
project_id_param = openapi.Parameter(
    "project_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True, description="Project ID"
)


def health_check(request):
    from django.http import JsonResponse
    return JsonResponse({"status": "ok"}, status=200)


def get_user_from_client_id(client_id):
    try:
        application = Application.objects.get(client_id=client_id)
        return application.user
    except Application.DoesNotExist:
        return None


class SubmitVersionAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Submit a new version snapshot for a file.",
        tags=["Document Versioning"],
        manual_parameters=[
            client_id_param, client_secret_param, file_id_param,
            openapi.Parameter("change_summary", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False, description="Summary of changes"),
        ],
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        file_id = request.query_params.get("file_id")
        change_summary = request.query_params.get("change_summary", "")

        if not all([client_id, client_secret, file_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        file_instance = get_object_or_404(File, id=file_id)

        if file_instance.user != user:
            raise PermissionDenied("You are not authorized to version this file.")

        if not os.path.exists(file_instance.filepath):
            return Response({"error": "File not found."}, status=status.HTTP_404_NOT_FOUND)

        version_number = get_next_version_number(file_instance)

        create_version_task.delay(file_id, str(file_instance.run_id))

        response_data = {
            "file_id": str(file_instance.id),
            "version_number": version_number,
            "status": "Processing"
        }

        return Response(response_data, status=status.HTTP_202_ACCEPTED)


class ListVersionsAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="List all version snapshots for a file.",
        tags=["Document Versioning"],
        manual_parameters=[client_id_param, client_secret_param, file_id_param],
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        file_id = request.query_params.get("file_id")

        if not all([client_id, client_secret, file_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        file_instance = get_object_or_404(File, id=file_id)

        if file_instance.user != user:
            raise PermissionDenied("You are not authorized to view versions of this file.")

        versions = DocumentVersion.objects.filter(file=file_instance).order_by("-version_number")

        return Response([
            {
                "id": str(v.id),
                "version_number": v.version_number,
                "filepath": get_logical_path(v.filepath),
                "file_hash": v.file_hash,
                "file_size": v.file_size,
                "mime_type": v.mime_type,
                "checksum_sha256": v.checksum_sha256,
                "is_archived": v.is_archived,
                "is_locked": v.is_locked,
                "change_summary": v.change_summary,
                "created_by": v.created_by,
                "created_at": v.created_at,
                "manifest_id": str(v.manifests.first().id) if v.manifests.exists() else None,
            }
            for v in versions
        ], status=status.HTTP_200_OK)


class GetVersionDiffAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Get the diff between two version snapshots.",
        tags=["Document Versioning"],
        manual_parameters=[client_id_param, client_secret_param, file_id_param, from_version_param, to_version_param],
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        file_id = request.query_params.get("file_id")
        from_version_id = request.query_params.get("from_version")
        to_version_id = request.query_params.get("to_version")

        if not all([client_id, client_secret, file_id, from_version_id, to_version_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        file_instance = get_object_or_404(File, id=file_id)

        if file_instance.user != user:
            raise PermissionDenied("You are not authorized to view diffs for this file.")

        from_ver = get_object_or_404(DocumentVersion, id=from_version_id, file=file_instance)
        to_ver = get_object_or_404(DocumentVersion, id=to_version_id, file=file_instance)

        diff = VersionDiff.objects.filter(
            file=file_instance,
            from_version=from_ver,
            to_version=to_ver,
        ).first()

        if not diff:
            return Response({"error": "Diff not found for the specified versions."}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "from_version": from_ver.version_number,
            "to_version": to_ver.version_number,
            "diff_content": diff.diff_content,
            "diff_html": diff.diff_html,
            "additions": diff.additions,
            "deletions": diff.deletions,
            "similarity_ratio": diff.similarity_ratio,
            "created_at": diff.created_at,
        }, status=status.HTTP_200_OK)


class CompareVersionsDetailedView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Get word-level diff between two versions with detailed statistics.",
        tags=["Document Versioning"],
        manual_parameters=[client_id_param, client_secret_param, file_id_param, from_version_param, to_version_param],
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        file_id = request.query_params.get("file_id")
        from_version_id = request.query_params.get("from_version")
        to_version_id = request.query_params.get("to_version")

        if not all([client_id, client_secret, file_id, from_version_id, to_version_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        file_instance = get_object_or_404(File, id=file_id)

        if file_instance.user != user:
            raise PermissionDenied("You are not authorized to compare versions for this file.")

        from_ver = get_object_or_404(DocumentVersion, id=from_version_id, file=file_instance)
        to_ver = get_object_or_404(DocumentVersion, id=to_version_id, file=file_instance)

        if not os.path.exists(from_ver.filepath) or not os.path.exists(to_ver.filepath):
            return Response({"error": "One or both version files not found on disk."}, status=status.HTTP_404_NOT_FOUND)

        try:
            diff_text, diff_html, additions, deletions, similarity_ratio = generate_diff(
                from_ver.filepath, to_ver.filepath
            )
        except Exception as e:
            return Response({"error": f"Failed to compute diff: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({
            "from_version": from_ver.version_number,
            "to_version": to_ver.version_number,
            "from_version_id": str(from_ver.id),
            "to_version_id": str(to_ver.id),
            "diff_text": diff_text,
            "diff_html": diff_html,
            "additions": additions,
            "deletions": deletions,
            "similarity_ratio": similarity_ratio,
            "from_file_size": from_ver.file_size,
            "to_file_size": to_ver.file_size,
        }, status=status.HTTP_200_OK)


class CheckVersionStatusAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Check the versioning status for a file or version.",
        tags=["Document Versioning"],
        manual_parameters=[client_id_param, client_secret_param, file_id_param],
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        file_id = request.query_params.get("file_id")
        version_id = request.query_params.get("version_id")

        if not all([client_id, client_secret]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        if version_id:
            version = get_object_or_404(DocumentVersion, id=version_id)
            return Response({
                "id": str(version.id),
                "file_id": str(version.file.id),
                "version_number": version.version_number,
                "filepath": get_logical_path(version.filepath),
                "file_hash": version.file_hash,
                "file_size": version.file_size,
                "mime_type": version.mime_type,
                "checksum_sha256": version.checksum_sha256,
                "is_archived": version.is_archived,
                "is_locked": version.is_locked,
                "created_by": version.created_by,
                "created_at": version.created_at,
            }, status=status.HTTP_200_OK)

        if file_id:
            file_instance = get_object_or_404(File, id=file_id)
            versions = DocumentVersion.objects.filter(file=file_instance).count()
            latest = DocumentVersion.objects.filter(file=file_instance).order_by("-version_number").first()
            policy = VersionRetentionPolicy.objects.filter(project_id=file_instance.project_id).first()
            return Response({
                "file_id": file_id,
                "total_versions": versions,
                "latest_version": latest.version_number if latest else None,
                "latest_version_id": str(latest.id) if latest else None,
                "latest_version_hash": latest.file_hash if latest else None,
                "retention_policy": {
                    "max_versions": policy.max_versions if policy else None,
                    "retention_days": policy.retention_days if policy else None,
                    "archive_after_days": policy.archive_after_days if policy else None,
                } if policy else None,
            }, status=status.HTTP_200_OK)

        return Response({"error": "Provide either file_id or version_id"}, status=status.HTTP_400_BAD_REQUEST)


class DownloadVersionAPIView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Download a specific version snapshot.",
        tags=["Document Versioning"],
        manual_parameters=[client_id_param, client_secret_param, version_id_param],
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        version_id = request.query_params.get("version_id")

        if not all([client_id, client_secret, version_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        version = get_object_or_404(DocumentVersion, id=version_id)

        if version.is_archived:
            archive_record = version.archive_records.first()
            if archive_record and os.path.exists(archive_record.archived_path):
                return Response({
                    "version_id": str(version.id),
                    "version_number": version.version_number,
                    "filepath": get_logical_path(archive_record.archived_path),
                    "file_hash": version.file_hash,
                    "is_archived": True,
                    "archive_format": archive_record.archive_format,
                    "created_at": version.created_at,
                }, status=status.HTTP_200_OK)
            return Response({"error": "Archived version file not found on disk."}, status=status.HTTP_404_NOT_FOUND)

        if not os.path.exists(version.filepath):
            return Response({"error": "Version file not found on disk."}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "version_id": str(version.id),
            "version_number": version.version_number,
            "filepath": get_logical_path(version.filepath),
            "file_hash": version.file_hash,
            "file_size": version.file_size,
            "mime_type": version.mime_type,
            "checksum_sha256": version.checksum_sha256,
            "is_archived": False,
            "created_at": version.created_at,
        }, status=status.HTTP_200_OK)


class RetentionPolicyView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Get or update retention policy for a project.",
        tags=["Document Versioning"],
        manual_parameters=[client_id_param, client_secret_param, project_id_param],
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        project_id = request.query_params.get("project_id")

        if not all([client_id, client_secret, project_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        policy = VersionRetentionPolicy.objects.filter(project_id=project_id).first()
        if not policy:
            return Response({"error": "No retention policy found for this project."}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "id": str(policy.id),
            "project_id": policy.project_id,
            "client_name": policy.client_name,
            "max_versions": policy.max_versions,
            "retention_days": policy.retention_days,
            "archive_after_days": policy.archive_after_days,
            "require_approval_for_delete": policy.require_approval_for_delete,
            "created_at": policy.created_at,
            "updated_at": policy.updated_at,
        }, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        operation_description="Create or update retention policy for a project.",
        tags=["Document Versioning"],
        manual_parameters=[client_id_param, client_secret_param, project_id_param],
    )
    def put(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        project_id = request.query_params.get("project_id")

        if not all([client_id, client_secret, project_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        policy, created = VersionRetentionPolicy.objects.update_or_create(
            project_id=project_id,
            defaults={
                "client_name": request.data.get("client_name", ""),
                "max_versions": request.data.get("max_versions", 10),
                "retention_days": request.data.get("retention_days", 365),
                "archive_after_days": request.data.get("archive_after_days", 180),
                "require_approval_for_delete": request.data.get("require_approval_for_delete", True),
            }
        )

        return Response({
            "id": str(policy.id),
            "project_id": policy.project_id,
            "client_name": policy.client_name,
            "max_versions": policy.max_versions,
            "retention_days": policy.retention_days,
            "archive_after_days": policy.archive_after_days,
            "require_approval_for_delete": policy.require_approval_for_delete,
            "created_at": policy.created_at,
            "updated_at": policy.updated_at,
        }, status=status.HTTP_200_OK if not created else status.HTTP_201_CREATED)


class VersionManifestView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Get the manifest for a specific version.",
        tags=["Document Versioning"],
        manual_parameters=[client_id_param, client_secret_param, version_id_param],
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        version_id = request.query_params.get("version_id")

        if not all([client_id, client_secret, version_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        version = get_object_or_404(DocumentVersion, id=version_id)

        if version.file.user != user:
            raise PermissionDenied("You are not authorized to view manifests for this file.")

        manifest = version.manifests.first()
        if not manifest:
            return Response({"error": "No manifest found for this version."}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "id": str(manifest.id),
            "version_id": str(manifest.version.id),
            "manifest_data": manifest.manifest_data,
            "checksum_algorithm": manifest.checksum_algorithm,
            "signature": manifest.signature,
            "signature_verified": manifest.signature_verified,
            "created_at": manifest.created_at,
        }, status=status.HTTP_200_OK)


class ArchiveVersionView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Archive a specific version.",
        tags=["Document Versioning"],
        manual_parameters=[client_id_param, client_secret_param, version_id_param],
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        version_id = request.query_params.get("version_id")

        if not all([client_id, client_secret, version_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        version = get_object_or_404(DocumentVersion, id=version_id)

        if version.file.user != user:
            raise PermissionDenied("You are not authorized to archive this version.")

        if version.is_archived:
            return Response({"error": "Version is already archived."}, status=status.HTTP_400_BAD_REQUEST)

        from document_versioning.utils import perform_archive
        archive_record = perform_archive(version)

        if not archive_record:
            return Response({"error": "Failed to archive version."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        version.is_archived = True
        version.save(update_fields=['is_archived'])

        if os.path.exists(version.filepath):
            os.remove(version.filepath)

        return Response({
            "version_id": str(version.id),
            "archive_id": str(archive_record.id),
            "archived_path": archive_record.archived_path,
            "archive_format": archive_record.archive_format,
            "size_bytes": archive_record.size_bytes,
            "archived_at": archive_record.archived_at,
        }, status=status.HTTP_200_OK)


class RestoreVersionView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Restore an archived version.",
        tags=["Document Versioning"],
        manual_parameters=[client_id_param, client_secret_param, version_id_param],
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        version_id = request.query_params.get("version_id")

        if not all([client_id, client_secret, version_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        version = get_object_or_404(DocumentVersion, id=version_id)

        if version.file.user != user:
            raise PermissionDenied("You are not authorized to restore this version.")

        if not version.is_archived:
            return Response({"error": "Version is not archived."}, status=status.HTTP_400_BAD_REQUEST)

        archive_record = version.archive_records.first()
        if not archive_record:
            return Response({"error": "No archive record found for this version."}, status=status.HTTP_404_NOT_FOUND)

        try:
            restored_by = str(user)
            restored_path = restore_from_archive(archive_record, restored_by)
        except FileNotFoundError as e:
            return Response({"error": str(e)}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "version_id": str(version.id),
            "restored_path": restored_path,
            "restored_at": archive_record.restored_at,
            "restored_by": archive_record.restored_by,
            "is_archived": version.is_archived,
        }, status=status.HTTP_200_OK)


class LockVersionView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Lock or unlock a version to prevent deletion.",
        tags=["Document Versioning"],
        manual_parameters=[client_id_param, client_secret_param, version_id_param],
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        version_id = request.query_params.get("version_id")

        if not all([client_id, client_secret, version_id]):
            return Response({"error": "Missing required parameters"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        version = get_object_or_404(DocumentVersion, id=version_id)

        if version.file.user != user:
            raise PermissionDenied("You are not authorized to lock this version.")

        lock = request.data.get("lock", True)
        version.is_locked = lock
        version.save(update_fields=['is_locked'])

        return Response({
            "version_id": str(version.id),
            "is_locked": version.is_locked,
        }, status=status.HTTP_200_OK)
