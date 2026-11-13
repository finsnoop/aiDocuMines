# views.py
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404
from django.http import FileResponse
from .models import (
    Folder, FileFolderLink, UserFileHide, UserFolderHide,
    FileColorTag, FolderColorTag, FileAlias, FolderAlias,
)
from .serializers import (
    FolderSerializer, FileFolderLinkSerializer,
    FileSerializer, EffectiveAccessSerializer, RecursiveFolderSerializer,
    FileColorTagSerializer, FolderColorTagSerializer,
    FileAliasSerializer, FolderAliasSerializer,
)
from core.models import File
from core.utils import get_logical_path
from custom_authentication.permissions import IsOwner, HasEffectiveAccess, HasAnyAccess, IsAdminOrSuperUser
from .models import COLOR_CHOICES
from custom_authentication.permissions import IsOwner, HasEffectiveAccess, IsClientOrAdminOrSuperUser
from oauth2_provider.contrib.rest_framework import OAuth2Authentication
from .utils import restore_file_from_trash, restore_folder_from_trash, delete_file
from .tasks import (
    async_bulk_trash_files,
    async_bulk_trash_folders,
    async_bulk_delete_folders,
    async_bulk_move_files_to_folder,
    async_rename_file,
    async_rename_folder,
    async_duplicate_file,
    async_copy_file,
    async_zip_files,
    async_password_protect_file,
)

import os
import mimetypes
import shutil
from document_operations.models import FileVersion
from django.conf import settings
from rest_framework.generics import RetrieveAPIView
from .models import FileFolderLink
from .serializers import FileFolderLinkSerializer
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi

from django.contrib.auth.models import Group
from .tasks import async_grant_public_link
from .utils import update_access_level_for_user, same_tenant
from .models import FileAccessEntry
from django.contrib.auth import get_user_model
from document_operations.utils import set_password_protection
from document_operations.models import FileAuditLog
from rest_framework.parsers import JSONParser, MultiPartParser, FormParser  # 🔁 Import this at the top

User = get_user_model()

'''
class FolderDetailView(APIView):
    permission_classes = [IsAuthenticated, HasEffectiveAccess]

    def get(self, request, pk):
        folder = get_object_or_404(Folder, pk=pk)
        self.check_object_permissions(request, folder)
        serializer = FolderSerializer(folder)
        return Response(serializer.data)
'''


class FolderDetailView(APIView):
    """
    GET /api/v1/documents/folders/<uuid>/

    Returns full recursive details of a single folder:
    - subfolders (nested)
    - files (optional, default true)

    Query-string support:
    ─────────────────────
    ?include_files=false  → omit the file listings
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, HasEffectiveAccess]

    def get(self, request, pk):
        folder = get_object_or_404(Folder, pk=pk)
        self.check_object_permissions(request, folder)

        include_files = request.query_params.get("include_files", "true").lower() == "true"
        ctx = {"include_files": include_files, "request": request}
        serializer = RecursiveFolderSerializer(folder, context=ctx)

        return Response(serializer.data, status=status.HTTP_200_OK)




class FileDetailView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, HasEffectiveAccess]

    def get(self, request, pk):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)
        serializer = FileSerializer(file_link.file)
        return Response(serializer.data)


class RenameFileView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, IsOwner, HasEffectiveAccess]

    def patch(self, request, pk):
        new_name = request.data.get("new_name")
        if not new_name:
            return Response({"error": "Missing new_name"}, status=400)

        # Audit log
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        FileAuditLog.objects.create(
            file=file_link.file,
            user=request.user,
            action="renamed",
            extra={"new_name": new_name}
        )

        async_rename_file.delay(file_link.file.id, new_name)
        return Response({"message": "File rename initiated."}, status=202)


class RenameFolderView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, IsOwner, HasEffectiveAccess]

    def patch(self, request, pk):
        new_name = request.data.get("new_name")
        if not new_name:
            return Response({"error": "Missing new_name"}, status=400)

        async_rename_folder.delay(pk, new_name)
        return Response({"message": "Folder rename initiated."}, status=202)


class TrashFilesView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def post(self, request):
        file_ids = request.data.get("file_ids", [])
        if not file_ids:
            return Response({"error": "Missing file_ids list."}, status=400)

        for fid in file_ids:
            link = get_object_or_404(FileFolderLink, pk=fid)
            self.check_object_permissions(request, link)

        async_bulk_trash_files.delay(file_ids)
        return Response({"message": "Files trash initiated."}, status=202)


class MoveFilesView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def post(self, request):
        file_ids = request.data.get("file_ids", [])
        target_folder = request.data.get("target_folder")
        if not file_ids or not target_folder:
            return Response({"error": "Missing file_ids or target_folder."}, status=400)

        for fid in file_ids:
            link = get_object_or_404(FileFolderLink, pk=fid)
            self.check_object_permissions(request, link)

        async_bulk_move_files_to_folder.delay(file_ids, target_folder)
        return Response({"message": "Files move initiated."}, status=202)


class DeleteFileView(APIView):
    """
    Permanently delete a file. Only the owner (or a user with effective access)
    can permanently delete their own trashed files.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, IsOwner, HasEffectiveAccess]

    def delete(self, request, pk):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)
        delete_file(file_link.id, actor=request.user)
        return Response({"message": "File deleted."}, status=200)


class DeleteFolderView(APIView):
    """
    Permanently delete a folder. Only the owner (or a user with effective access)
    can permanently delete their own trashed folders.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, IsOwner, HasEffectiveAccess]

    def delete(self, request, pk):
        import os
        from core.models import File as CoreFile

        folder = get_object_or_404(Folder, pk=pk)
        self.check_object_permissions(request, folder)

        # Collect all file paths before cascade delete removes the DB rows
        filepaths = self._collect_folder_filepaths(folder)
        # Collect child folder filepaths too (Django cascade deletes subfolders)
        for child in folder.subfolders.all():
            filepaths.extend(self._collect_folder_filepaths(child))

        folder.delete()

        # Remove physical files from disk
        for fpath in filepaths:
            if fpath and os.path.isfile(fpath):
                try:
                    os.remove(fpath)
                except OSError:
                    pass

        return Response({"message": "Folder deleted."}, status=200)

    def _collect_folder_filepaths(self, folder):
        """Recursively collect absolute file paths from a folder and its subfolders."""
        paths = []
        for link in folder.files.select_related('file').all():
            if link.file.filepath:
                paths.append(link.file.filepath)
        for child in folder.subfolders.all():
            paths.extend(self._collect_folder_filepaths(child))
        return paths


class DuplicateFileView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, IsOwner, HasEffectiveAccess]

    def post(self, request, pk):
        async_duplicate_file.delay(pk)
        return Response({"message": "File duplication initiated."}, status=202)


class CopyFileView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, IsOwner, HasEffectiveAccess]

    def post(self, request, pk):
        target_folder = request.data.get("target_folder")
        if not target_folder:
            return Response({"error": "Missing target_folder"}, status=400)

        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)
        async_copy_file.delay(file_link.file.id, target_folder)
        return Response({"message": "File copy initiated."}, status=202)


class ZipFilesView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def post(self, request):
        file_ids = request.data.get("file_ids", [])
        if not file_ids:
            return Response({"error": "Missing file_ids"}, status=400)

        async_zip_files.delay(file_ids)
        return Response({"message": "Zip operation initiated."}, status=202)


class ProtectFileView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, HasEffectiveAccess]

    def post(self, request, pk):
        password_hint = request.data.get("password_hint")
        if not password_hint:
            return Response({"error": "Missing password_hint"}, status=400)

        # Audit log
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        FileAuditLog.objects.create(
            file=file_link.file,
            user=request.user,
            action="updated",
            extra={"protection": "password", "hint": password_hint}
        )

        async_password_protect_file.delay(pk, password_hint)
        return Response({"message": "Password protection initiated."}, status=202)


class RestoreFileView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, HasEffectiveAccess]

    def post(self, request, pk):
        restore_file_from_trash(pk, actor=request.user)
        return Response({"message": "File restored."}, status=status.HTTP_200_OK)


class RestoreFolderView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, HasEffectiveAccess]

    def post(self, request, pk):
        result = restore_folder_from_trash(pk)
        return Response(result, status=status.HTTP_200_OK)


class ListFileVersionsView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, HasEffectiveAccess]

    def get(self, request, pk):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)
        versions = file_link.file.versions.all()
        data = [{
            "version": v.version_number,
            "uploaded_at": v.uploaded_at,
            "uploaded_by": v.uploaded_by.email if v.uploaded_by else "Unknown",
            "file_path": get_logical_path(v.file_path),
        } for v in versions]
        return Response(data, status=200)


class RestoreFileVersionView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, HasEffectiveAccess]

    def post(self, request, pk, version_number):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)
        file = file_link.file

        try:
            version = get_object_or_404(file.versions, version_number=version_number)
        except FileVersion.DoesNotExist:
            return Response({"error": "Version not found."}, status=status.HTTP_404_NOT_FOUND)

        # Ensure the source file path exists
        if not os.path.exists(version.file_path):
            return Response({"error": "Version file is missing on disk."}, status=status.HTTP_404_NOT_FOUND)

        # Create a backup of the current file (optional)
        backup_dir = os.path.join(settings.MEDIA_ROOT, "backups", str(file.id))
        os.makedirs(backup_dir, exist_ok=True)

        backup_path = os.path.join(backup_dir, f"backup_{file.filename}")
        if os.path.exists(file.file.path):
            shutil.copy(file.file.path, backup_path)

        try:
            # Restore the selected version (overwrite the current file)
            shutil.copy(version.file_path, file.file.path)
        except Exception as e:
            return Response({"error": f"Restore failed: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({
            "message": f"Successfully restored file to version {version_number}.",
            "backup_created_at": backup_path
        }, status=status.HTTP_200_OK)


class SharedFilesView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def get(self, request):
        client_id = getattr(request.user, "client_id", None)
        access_links = FileAccessEntry.objects.filter(user=request.user).select_related("file_link__file__user")
        if not client_id:
            access_links = access_links.none()
        else:
            access_links = access_links.filter(file_link__file__user__client_id=client_id)
        file_links = [entry.file_link for entry in access_links if entry.file_link]
        serializer = FileFolderLinkSerializer(file_links, many=True)
        return Response(serializer.data)


'''
class ShareFileView(APIView):
    permission_classes = [IsAuthenticated, HasEffectiveAccess]

    def post(self, request, pk):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)

        user_ids = request.data.get("user_ids", [])
        if not user_ids:
            return Response({"error": "No users provided."}, status=400)

        for uid in user_ids:
            user = get_object_or_404(User, pk=uid)
            file_link.shared_with.add(user)

        file_link.is_shared = True
        file_link.save()

        return Response({"message": "File shared successfully."}, status=200)
'''


class ShareFileView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, HasEffectiveAccess]
    parser_classes = [JSONParser]

    @swagger_auto_schema(
        operation_description="Share a file with users by ID or email",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                "user_ids": openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Items(type=openapi.TYPE_INTEGER)),
                "emails": openapi.Schema(type=openapi.TYPE_ARRAY, items=openapi.Items(type=openapi.TYPE_STRING)),
                "access_level": openapi.Schema(type=openapi.TYPE_STRING, enum=["read", "write", "owner"], default="read")
            }
        ),
        responses={200: "Success"}
    )
    def post(self, request, pk):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)

        user_ids = request.data.get("user_ids", []) or []
        emails = request.data.get("emails", []) or []
        level = request.data.get("access_level", "read")
        owner = file_link.file.user
        if not same_tenant(request.user, owner):
            return Response({"error": "You can only share files inside your organisation."}, status=403)

        access_map = {
            "read":  {"can_read": True},
            "write": {"can_read": True, "can_write": True},
            "owner": {"can_read": True, "can_write": True, "can_delete": True, "can_share": True}
        }
        perms = access_map.get(level, {"can_read": True})
        client_id = owner.client_id

        # Resolve emails to user IDs
        # Multi-tenancy: documents can only be shared with users of the same organisation.
        from core.access import scope_users
        tenant_users = scope_users(User.objects.all(), request.user)
        if user_ids:
            try:
                requested_ids = {int(u) for u in user_ids}
            except (TypeError, ValueError):
                return Response({"error": "Invalid user id."}, status=400)
            allowed_ids = set(tenant_users.filter(id__in=requested_ids).values_list("id", flat=True))
            if allowed_ids != requested_ids:
                return Response({"error": "One or more users were not found in your organisation."}, status=404)
            user_ids = list(requested_ids)
        if emails:
            resolved = list(tenant_users.filter(email__in=emails).values_list("id", flat=True))
            user_ids = list(set(user_ids + resolved))
            unresolved = set(emails) - set(tenant_users.filter(email__in=emails).values_list("email", flat=True))
            if unresolved:
                return Response({"error": "Users not found in your organisation."}, status=404)

        if not user_ids:
            return Response({"error": "No users provided."}, status=400)

        recipients = []
        for uid in user_ids:
            user = get_object_or_404(User, pk=uid, client_id=client_id)
            if not same_tenant(owner, user):
                return Response({"error": "Documents can only be shared with users in the same organisation."}, status=403)
            FileAccessEntry.objects.update_or_create(
                file_link=file_link,
                user=user,
                defaults={**perms, "granted_by": request.user}
            )
            recipients.append(user.id)

        file_link.is_shared = True
        file_link.save()

        return Response({
            "message": "File shared successfully.",
            "shared_with": recipients,
            "access_level": level
        }, status=200)


class UnshareFileView(APIView):
    permission_classes = [IsAuthenticated, HasEffectiveAccess]

    def post(self, request, pk):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)

        FileAccessEntry.objects.filter(file_link=file_link).delete()
        file_link.is_shared = False
        file_link.save()

        return Response({"message": "File unshared successfully."}, status=200)


class FilePreviewView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, HasEffectiveAccess]

    def get(self, request, pk):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)
        file_obj = file_link.file

        real_path = file_obj.filepath
        if not real_path or not os.path.exists(real_path):
            return Response({"error": "File not found on storage"}, status=404)

        content_type, _ = mimetypes.guess_type(real_path)
        if not content_type:
            content_type = "application/octet-stream"

        filename = os.path.basename(real_path)

        try:
            return FileResponse(
                open(real_path, "rb"),
                as_attachment=False,
                filename=filename,
                content_type=content_type
            )
        except Exception as e:
            return Response({"error": f"Failed to read file: {str(e)}"}, status=500)



class FileAuditLogView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, HasEffectiveAccess]

    def get(self, request, pk):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)

        # Logs from FileAuditLog model
        logs = FileAuditLog.objects.filter(file_id=file_link.file.id).order_by("-timestamp")

        # Access entries from sharing
        access_entries = FileAccessEntry.objects.filter(file_link__file_id=file_link.file.id)

        log_data = [
            {
                "type": "Log",
                "action": log.action,
                "user": log.user.email if log.user else None,
                "extra": log.extra,
                "timestamp": log.timestamp,
            }
            for log in logs
        ]

        access_data = [
            {
                "type": "Access",
                "user": e.user.email,
                "granted_by": e.granted_by.email if e.granted_by else None,
                "access": {
                    "read": e.can_read,
                    "write": e.can_write,
                    "share": e.can_share,
                    "owner": getattr(e, "is_owner", False),  # fallback safe
                },
                "granted_at": e.granted_at,
            }
            for e in access_entries
        ]

        return Response(log_data + access_data, status=200)



'''
class FileAuditLogView(APIView):
    permission_classes = [IsAuthenticated, HasEffectiveAccess]

    def get(self, request, pk):
        logs = FileAuditLog.objects.filter(file_id=pk).order_by("-timestamp")
        access_entries = FileAccessEntry.objects.filter(file_link__file_id=pk)

        access_log_data = [
            {
                "type": "Access",
                "user": e.user.email,
                "granted_by": e.granted_by.email if e.granted_by else None,
                "access": {
                    "read": e.can_read,
                    "write": e.can_write,
                    "share": e.can_share,
                    "owner": e.can_write and e.can_share and not e.expires_at,  # inferred owner
                },
                "granted_at": e.granted_at
            }
            for e in access_entries
        ]

        file_logs = [{
            "type": "Log",
            "action": log.action,
            "user": log.user.email,
            "timestamp": log.timestamp,
        } for log in logs]

        return Response(file_logs + access_log_data, status=200)
'''

class HideFileView(APIView):
    permission_classes = [IsAuthenticated, HasAnyAccess]

    def post(self, request, pk):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)
        UserFileHide.objects.get_or_create(user=request.user, file_link=file_link)
        return Response({"message": "File hidden from your view."}, status=200)


class UnhideFileView(APIView):
    permission_classes = [IsAuthenticated, HasAnyAccess]

    def post(self, request, pk):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)
        UserFileHide.objects.filter(user=request.user, file_link=file_link).delete()
        return Response({"message": "File unhidden."}, status=200)


class HideFolderView(APIView):
    permission_classes = [IsAuthenticated, HasAnyAccess]

    def post(self, request, pk):
        folder = get_object_or_404(Folder, pk=pk)
        self.check_object_permissions(request, folder)
        UserFolderHide.objects.get_or_create(user=request.user, folder=folder)
        return Response({"message": "Folder hidden from your view."}, status=200)


class UnhideFolderView(APIView):
    permission_classes = [IsAuthenticated, HasAnyAccess]

    def post(self, request, pk):
        folder = get_object_or_404(Folder, pk=pk)
        self.check_object_permissions(request, folder)
        UserFolderHide.objects.filter(user=request.user, folder=folder).delete()
        return Response({"message": "Folder unhidden."}, status=200)


class BulkTrashFoldersView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        folder_ids = request.data.get("folder_ids", [])
        if not folder_ids:
            return Response({"error": "Missing folder_ids list."}, status=400)
        async_bulk_trash_folders.delay(folder_ids)
        return Response({"message": "Bulk folder trash initiated."}, status=202)


class BulkRestoreFoldersView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        folder_ids = request.data.get("folder_ids", [])
        if not folder_ids:
            return Response({"error": "Missing folder_ids list."}, status=400)
        from document_operations.utils import restore_folder_recursive
        from document_operations.models import Folder
        results = []
        for fid in folder_ids:
            try:
                folder = Folder.objects.get(id=fid)
                restore_folder_recursive(folder)
                results.append(f"Restored {folder.name}")
            except Folder.DoesNotExist:
                results.append(f"Folder {fid} not found")
        return Response({"message": results}, status=status.HTTP_200_OK)


class BulkDeleteFoldersView(APIView):
    permission_classes = [IsAuthenticated, IsAdminOrSuperUser]

    def post(self, request):
        folder_ids = request.data.get("folder_ids", [])
        if not folder_ids:
            return Response({"error": "Missing folder_ids list."}, status=400)
        async_bulk_delete_folders.delay(folder_ids)
        return Response({"message": "Bulk folder delete initiated."}, status=202)


class SharedTreeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        client_id = getattr(request.user, "client_id", None)
        access_entries = FileAccessEntry.objects.filter(user=request.user).select_related(
            'file_link__file__user', 'file_link__folder'
        )
        if not client_id:
            access_entries = access_entries.none()
        else:
            access_entries = access_entries.filter(file_link__file__user__client_id=client_id)
        owner_map = {}
        for entry in access_entries:
            link = entry.file_link
            if not link:
                continue
            owner_key = link.file.user.email if link.file and link.file.user else "unknown"
            folder = link.folder
            if not folder:
                continue
            if owner_key not in owner_map:
                owner_map[owner_key] = {"name": owner_key, "type": "folder", "project_id": "shared", "service_id": "shared", "children": []}
            folder_path = []
            cur = folder
            while cur:
                folder_path.insert(0, cur)
                cur = cur.parent
            current_list = owner_map[owner_key]["children"]
            for depth_folder in folder_path:
                existing = next((c for c in current_list if c.get("name") == depth_folder.name and c.get("id") == str(depth_folder.id)), None)
                if not existing:
                    existing = {
                        "id": str(depth_folder.id),
                        "name": depth_folder.name,
                        "type": "folder",
                        "project_id": depth_folder.project_id,
                        "service_id": depth_folder.service_id,
                        "children": []
                    }
                    current_list.append(existing)
                current_list = existing["children"]
            current_list.append({
                "id": link.id,
                "file_id": link.file.id,
                "file_name": link.file.filename,
                "type": "file",
                "is_shared": True,
                "owner": owner_key,
                "folder": str(folder.id),
                "is_trashed": link.is_trashed,
                "created_at": link.file.created_at,
                "file_size": link.file.file_size,
                "file_type": link.file.file_type,
            })
        return Response(list(owner_map.values()), status=200)


class PublicSharedFileView(RetrieveAPIView):
    permission_classes = []  # public access

    def get(self, request, share_token):
        link = get_object_or_404(FileFolderLink, public_token=share_token, is_public=True)
        serializer = FileFolderLinkSerializer(link)
        return Response({
            "shared_file": serializer.data,
            "preview_url": link.file.file.url  # Optional: direct link
        })

'''
# document_operations/views.py
class FolderListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        folders = Folder.objects.filter(user=request.user)
        data = [{"id": str(f.id), "name": f.name, "project_id": f.project_id} for f in folders]
        return Response(data)
'''

# document_operations/views.py
class FolderListView(APIView):
    """
    GET /api/v1/documents/folders/

    Query-string filters
    ─────────────────────
    ?project_id=<str>      → return only this project’s root folders
    ?service_id=<str>      → narrow further by service
    ?include_trashed=true  → include folders that are in the trash
    ?include_files=false   → omit the file listings inside each folder
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def get(self, request):
        # ── base queryset: current user + root-level folders
        qs = Folder.objects.filter(user=request.user, parent__isnull=True)

        # ── optional query-string filters
        project_id      = request.query_params.get("project_id")
        service_id      = request.query_params.get("service_id")
        include_trashed = request.query_params.get("include_trashed", "false").lower() == "true"
        include_files   = request.query_params.get("include_files",  "true").lower() == "true"

        if project_id:
            qs = qs.filter(project_id=project_id)

        if service_id:
            qs = qs.filter(service_id=service_id)

        if not include_trashed:
            qs = qs.filter(is_trashed=False)

        # ── serializer context lets us tell the recursive serializer
        #    whether to include the file list for each folder.
        ctx = {"include_files": include_files, "request": request}

        serializer = RecursiveFolderSerializer(qs, many=True, context=ctx)
        return Response(serializer.data, status=status.HTTP_200_OK)


class FolderCreateView(APIView):
    """
    POST /api/v1/documents/folders/

    Body:
    {
      "name": "child_folder",
      "project_id": "test_project",
      "service_id": "test_service",
      "parent": "<optional_parent_uuid>"
    }
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def post(self, request):
        data = request.data.copy()
        data['user'] = request.user.id  # bind folder to user

        serializer = FolderSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        folder = serializer.save()
        return Response(FolderSerializer(folder).data, status=status.HTTP_201_CREATED)




class TrashSingleFileView(APIView):
    """
    PATCH /api/v1/documents/files/<pk>/trash/

    Moves a single file to trash.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, HasEffectiveAccess]

    def patch(self, request, pk):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)
        file_link.is_trashed = True
        file_link.save()
        return Response({"message": f"File {pk} moved to trash."}, status=200)


class TrashFolderView(APIView):
    """
    PATCH /api/v1/documents/folders/<uuid:pk>/trash/

    Moves a single folder to trash.  Idempotent – if already trashed
    the call still succeeds so that the frontend can safely retry.
    """
    permission_classes = [IsAuthenticated, HasEffectiveAccess]

    def patch(self, request, pk):
        folder = get_object_or_404(Folder, pk=pk)
        self.check_object_permissions(request, folder)
        if not folder.is_trashed:
            folder.is_trashed = True
            folder.save()
        return Response({"message": f"Folder {pk} moved to trash."}, status=200)


class FolderListCreateView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def get(self, request):
        qs = Folder.objects.filter(user=request.user, parent__isnull=True)

        project_id      = request.query_params.get("project_id")
        service_id      = request.query_params.get("service_id")
        include_trashed = request.query_params.get("include_trashed", "false").lower() == "true"
        include_files   = request.query_params.get("include_files",  "true").lower() == "true"

        if project_id:
            qs = qs.filter(project_id=project_id)

        if service_id:
            qs = qs.filter(service_id=service_id)

        if not include_trashed:
            qs = qs.filter(is_trashed=False)

        ctx = {
            "include_files": include_files,
            "include_trashed": include_trashed,
            "request": request
        }
        serializer = RecursiveFolderSerializer(qs, many=True, context=ctx)
        return Response(serializer.data, status=200)

    def post(self, request):
        data = request.data.copy()
        data['user'] = request.user.id
        serializer = FolderSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        folder = serializer.save()
        return Response(FolderSerializer(folder).data, status=201)



class CreateChildFolderView(APIView):
    """
    POST /api/v1/document-operations/folders/create-child/

    Creates a child folder under a parent folder.
    This is specifically designed for service execution outputs.

    Request Body:
    {
        "parent_folder_id": "uuid",
        "folder_name": "Intake_Triage_20241122_143022",
        "project_id": "optional_project_id",
        "service_id": "optional_service_id"
    }

    Response:
    {
        "id": "uuid",
        "name": "Intake_Triage_20241122_143022",
        "parent": "parent_uuid",
        "project_id": "...",
        "service_id": "...",
        "created_at": "2024-11-22T14:30:22Z"
    }
    """
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Create a child folder under a parent folder for service execution outputs",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            required=['parent_folder_id', 'folder_name'],
            properties={
                'parent_folder_id': openapi.Schema(type=openapi.TYPE_STRING, description='UUID of parent folder'),
                'folder_name': openapi.Schema(type=openapi.TYPE_STRING, description='Name of the new child folder'),
                'project_id': openapi.Schema(type=openapi.TYPE_STRING, description='Optional project ID'),
                'service_id': openapi.Schema(type=openapi.TYPE_STRING, description='Optional service ID'),
            }
        ),
        responses={
            201: FolderSerializer(),
            400: "Bad Request - Missing required fields or invalid parent folder",
            404: "Parent folder not found"
        }
    )
    def post(self, request):
        try:
            # Extract request data
            parent_folder_id = request.data.get('parent_folder_id')
            folder_name = request.data.get('folder_name')
            project_id = request.data.get('project_id')
            service_id = request.data.get('service_id')

            # Validate required fields
            if not parent_folder_id or not folder_name:
                return Response({
                    'error': 'Missing required fields: parent_folder_id and folder_name are required'
                }, status=status.HTTP_400_BAD_REQUEST)

            # Get parent folder
            try:
                parent_folder = Folder.objects.get(pk=parent_folder_id, user=request.user)
            except Folder.DoesNotExist:
                return Response({
                    'error': f'Parent folder with ID {parent_folder_id} not found or you do not have access'
                }, status=status.HTTP_404_NOT_FOUND)

            # Inherit project_id and service_id from parent if not provided
            if not project_id:
                project_id = parent_folder.project_id
            if not service_id:
                service_id = parent_folder.service_id

            # Create child folder
            child_folder = Folder.objects.create(
                name=folder_name,
                parent=parent_folder,
                user=request.user,
                project_id=project_id,
                service_id=service_id
            )

            # Serialize and return
            serializer = FolderSerializer(child_folder)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        except Exception as e:
            return Response({
                'error': f'Failed to create child folder: {str(e)}'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class UploadFileToFolderView(APIView):
    """
    POST /api/v1/document-operations/files/upload-to-folder/

    Uploads a file and links it to a specific folder.
    This is specifically designed for service execution output files.

    Request (multipart/form-data):
    - file: File to upload
    - folder_id: UUID of the folder to upload to
    - filename: Optional custom filename

    Response:
    {
        "id": "file_id",
        "filename": "triage_report.pdf",
        "folder_id": "folder_uuid",
        "file_size": 2457600,
        "file_type": "pdf",
        "download_url": "https://...",
        "created_at": "2024-11-22T14:30:22Z"
    }
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @swagger_auto_schema(
        operation_description="Upload a file to a specific folder",
        manual_parameters=[
            openapi.Parameter('file', openapi.IN_FORM, type=openapi.TYPE_FILE, required=True, description='File to upload'),
            openapi.Parameter('folder_id', openapi.IN_FORM, type=openapi.TYPE_STRING, required=True, description='UUID of folder'),
            openapi.Parameter('filename', openapi.IN_FORM, type=openapi.TYPE_STRING, required=False, description='Custom filename'),
        ],
        responses={
            201: openapi.Schema(
                type=openapi.TYPE_OBJECT,
                properties={
                    'id': openapi.Schema(type=openapi.TYPE_INTEGER),
                    'filename': openapi.Schema(type=openapi.TYPE_STRING),
                    'folder_id': openapi.Schema(type=openapi.TYPE_STRING),
                    'file_size': openapi.Schema(type=openapi.TYPE_INTEGER),
                    'file_type': openapi.Schema(type=openapi.TYPE_STRING),
                    'download_url': openapi.Schema(type=openapi.TYPE_STRING),
                }
            ),
            400: "Bad Request - Missing file or folder_id",
            404: "Folder not found"
        }
    )
    def post(self, request):
        try:
            # Extract request data
            uploaded_file = request.FILES.get('file')
            folder_id = request.data.get('folder_id')
            custom_filename = request.data.get('filename')

            # Validate required fields
            if not uploaded_file:
                return Response({
                    'error': 'Missing required field: file'
                }, status=status.HTTP_400_BAD_REQUEST)

            if not folder_id:
                return Response({
                    'error': 'Missing required field: folder_id'
                }, status=status.HTTP_400_BAD_REQUEST)

            # Get folder
            try:
                folder = Folder.objects.get(pk=folder_id, user=request.user)
            except Folder.DoesNotExist:
                return Response({
                    'error': f'Folder with ID {folder_id} not found or you do not have access'
                }, status=status.HTTP_404_NOT_FOUND)

            # Determine filename
            filename = custom_filename if custom_filename else uploaded_file.name

            # Save file to storage
            import os
            from django.conf import settings
            import uuid as uuid_lib

            # Create upload directory if it doesn't exist
            upload_dir = os.path.join(settings.MEDIA_ROOT, 'service_outputs', str(request.user.id), str(folder_id))
            os.makedirs(upload_dir, exist_ok=True)

            # Generate unique filename to avoid collisions
            file_extension = os.path.splitext(filename)[1]
            unique_filename = f"{uuid_lib.uuid4()}{file_extension}"
            file_path = os.path.join(upload_dir, unique_filename)

            # Write file to disk
            with open(file_path, 'wb+') as destination:
                for chunk in uploaded_file.chunks():
                    destination.write(chunk)

            # Get file size
            file_size = os.path.getsize(file_path)

            # Determine file type
            file_type = file_extension.replace('.', '').lower() if file_extension else 'unknown'

            # Create File record
            file_record = File.objects.create(
                user=request.user,
                filename=filename,
                filepath=file_path,
                file_size=file_size,
                file_type=file_type,
                status='Uploaded'
            )

            # Create FileFolderLink to associate file with folder
            FileFolderLink.objects.create(
                file=file_record,
                folder=folder
            )

            # Generate download URL
            download_url = f"/api/v1/core/files/{file_record.id}/download/"

            # Return response
            return Response({
                'id': file_record.id,
                'filename': file_record.filename,
                'folder_id': str(folder_id),
                'file_size': file_size,
                'file_type': file_type,
                'download_url': download_url,
                'created_at': file_record.created_at.isoformat()
            }, status=status.HTTP_201_CREATED)

        except Exception as e:
            import traceback
            return Response({
                'error': f'Failed to upload file: {str(e)}',
                'traceback': traceback.format_exc()
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ShareWithGroupView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, HasEffectiveAccess]

    @swagger_auto_schema(
        operation_description="Share file with a group",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                "group_name": openapi.Schema(type=openapi.TYPE_STRING),
                "access_level": openapi.Schema(type=openapi.TYPE_STRING, enum=["read", "write", "owner"]),
            }
        ),
        responses={200: "Shared successfully"}
    )
    def post(self, request, pk):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)

        group_name = request.data.get("group_name")
        level = request.data.get("access_level", "read")
        group = get_object_or_404(Group, name=group_name)
        owner = file_link.file.user
        if not same_tenant(request.user, owner):
            return Response({"error": "You can only share files inside your organisation."}, status=403)

        perms = {
            "read":  {"can_read": True},
            "write": {"can_read": True, "can_write": True},
            "owner": {"can_read": True, "can_write": True, "can_share": True, "can_delete": True}
        }.get(level, {"can_read": True})

        recipients = [
            user for user in group.user_set.all()
            if same_tenant(owner, user)
        ]
        if not recipients:
            return Response({"error": "No users in that group belong to your organisation."}, status=403)

        for user in recipients:
            FileAccessEntry.objects.update_or_create(
                file_link=file_link,
                user=user,
                defaults={**perms, "granted_by": request.user}
            )

        file_link.is_shared = True
        file_link.save()

        return Response({
            "message": f"Shared with group {group_name}",
            "shared_with": [user.id for user in recipients],
        }, status=200)


class SetAccessLevelView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, HasEffectiveAccess]

    @swagger_auto_schema(
        operation_description="Update a user's access level for a file",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={
                "user_id": openapi.Schema(type=openapi.TYPE_INTEGER),
                "access_level": openapi.Schema(type=openapi.TYPE_STRING, enum=["read", "write", "owner"]),
            }
        ),
        responses={200: "Updated"}
    )
    def patch(self, request, pk):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)

        user_id = request.data.get("user_id")
        level = request.data.get("access_level", "read")

        result = update_access_level_for_user(file_link, user_id, level, request.user)
        return Response(result, status=200)


class GrantPublicLinkView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser, HasEffectiveAccess]

    def post(self, request, pk):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)

        FileAuditLog.objects.create(
            file=file_link.file,
            user=request.user,
            action="shared",
            extra={"shared_as": "public"}
        )

        async_grant_public_link.delay(file_link.id)
        return Response({"message": "Public access link is being generated."}, status=202)


class FileColorTagView(APIView):
    permission_classes = [IsAuthenticated, HasEffectiveAccess]

    @swagger_auto_schema(
        operation_description="Set or update color tag on a file",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={'color': openapi.Schema(type=openapi.TYPE_STRING, description='red|green|blue|orange|purple|grey|yellow')}
        ),
        responses={200: FileColorTagSerializer()}
    )
    def post(self, request, pk):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)
        color = request.data.get('color')
        if color not in dict(COLOR_CHOICES):
            return Response({'error': f'Invalid color. Choices: {", ".join(dict(COLOR_CHOICES).keys())}'}, status=400)
        tag, created = FileColorTag.objects.update_or_create(
            file_link=file_link,
            defaults={'color': color}
        )
        serializer = FileColorTagSerializer(tag)
        return Response(serializer.data, status=201 if created else 200)

    def delete(self, request, pk):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)
        FileColorTag.objects.filter(file_link=file_link).delete()
        return Response({'message': 'Color tag removed'}, status=200)


class FolderColorTagView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Set or update color tag on a folder",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={'color': openapi.Schema(type=openapi.TYPE_STRING, description='red|green|blue|orange|purple|grey|yellow')}
        ),
        responses={200: FolderColorTagSerializer()}
    )
    def post(self, request, pk):
        folder = get_object_or_404(Folder, pk=pk)
        color = request.data.get('color')
        if color not in dict(COLOR_CHOICES):
            return Response({'error': f'Invalid color. Choices: {", ".join(dict(COLOR_CHOICES).keys())}'}, status=400)
        tag, created = FolderColorTag.objects.update_or_create(
            folder=folder,
            defaults={'color': color}
        )
        serializer = FolderColorTagSerializer(tag)
        return Response(serializer.data, status=201 if created else 200)

    def delete(self, request, pk):
        folder = get_object_or_404(Folder, pk=pk)
        FolderColorTag.objects.filter(folder=folder).delete()
        return Response({'message': 'Color tag removed'}, status=200)


class FileAliasView(APIView):
    permission_classes = [IsAuthenticated, HasEffectiveAccess]

    @swagger_auto_schema(
        operation_description="Create an alias for a file",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={'alias_name': openapi.Schema(type=openapi.TYPE_STRING, description='Alias name for the file')}
        ),
        responses={201: FileAliasSerializer()}
    )
    def post(self, request, pk):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)
        alias_name = request.data.get('alias_name')
        if not alias_name:
            return Response({'error': 'alias_name is required'}, status=400)
        alias = FileAlias.objects.create(
            file_link=file_link,
            alias_name=alias_name,
            created_by=request.user
        )
        serializer = FileAliasSerializer(alias)
        return Response(serializer.data, status=201)

    def get(self, request, pk):
        file_link = get_object_or_404(FileFolderLink, pk=pk)
        self.check_object_permissions(request, file_link)
        aliases = file_link.aliases.all()
        serializer = FileAliasSerializer(aliases, many=True)
        return Response(serializer.data)


class FileAliasDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk, alias_id):
        alias = get_object_or_404(FileAlias, pk=alias_id, file_link_id=pk)
        alias.delete()
        return Response({'message': 'Alias deleted'}, status=200)


class FolderAliasView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_description="Create an alias for a folder",
        request_body=openapi.Schema(
            type=openapi.TYPE_OBJECT,
            properties={'alias_name': openapi.Schema(type=openapi.TYPE_STRING, description='Alias name for the folder')}
        ),
        responses={201: FolderAliasSerializer()}
    )
    def post(self, request, pk):
        folder = get_object_or_404(Folder, pk=pk)
        alias_name = request.data.get('alias_name')
        if not alias_name:
            return Response({'error': 'alias_name is required'}, status=400)
        alias = FolderAlias.objects.create(
            folder=folder,
            alias_name=alias_name,
            created_by=request.user
        )
        serializer = FolderAliasSerializer(alias)
        return Response(serializer.data, status=201)

    def get(self, request, pk):
        folder = get_object_or_404(Folder, pk=pk)
        aliases = folder.aliases.all()
        serializer = FolderAliasSerializer(aliases, many=True)
        return Response(serializer.data)


class FolderAliasDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk, alias_id):
        alias = get_object_or_404(FolderAlias, pk=alias_id, folder_id=pk)
        alias.delete()
        return Response({'message': 'Alias deleted'}, status=200)

