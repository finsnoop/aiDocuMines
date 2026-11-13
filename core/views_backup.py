"""
Platform database backups (platform super-admins only).

    GET    /api/v1/core/backups/                        list with status and size
    POST   /api/v1/core/backups/                        start a backup (runs in the background)
    GET    /api/v1/core/backups/<name>/download/        download the dump
    DELETE /api/v1/core/backups/<name>/                 delete
    POST   /api/v1/core/backups/<name>/restore/         {"confirm": "RESTORE <name>"}; needs ENABLE_BACKUP_RESTORE=true
"""
import os

from django.http import FileResponse, Http404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import is_platform_superuser
from core.backups import (backup_dir, create_database_backup, list_backups, new_backup_name, read_meta,
                          restore_database_backup, valid_name, write_meta)


def _restore_enabled():
    return os.getenv("ENABLE_BACKUP_RESTORE", "false").strip().lower() in ("1", "true", "yes", "on")


def _audit(request, action, name, **details):
    from custom_authentication.views_admin import audit
    audit(request, action, "backup", name, name, client_id=None, **details)


class _PlatformOnly(APIView):
    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not is_platform_superuser(request.user):
            raise Http404()  # do not reveal the feature to organisation admins

    def _existing(self, name):
        if not valid_name(name) or not os.path.exists(os.path.join(backup_dir(), name)):
            raise Http404("Backup not found")


class BackupListView(_PlatformOnly):
    def get(self, request):
        return Response({"results": list_backups(), "restore_enabled": _restore_enabled(),
                         "note": "Database only. Back up the document storage volume with your storage snapshots."})

    def post(self, request):
        running = [b for b in list_backups() if b.get("status") == "running"]
        if running:
            return Response({"error": "A backup is already running."}, status=status.HTTP_409_CONFLICT)
        name = new_backup_name()
        write_meta(name, status="queued", requested_by=request.user.email)
        create_database_backup.delay(name, request.user.email)
        _audit(request, "backup.created", name)
        return Response(read_meta(name), status=status.HTTP_202_ACCEPTED)


class BackupDetailView(_PlatformOnly):
    def delete(self, request, name):
        self._existing(name)
        os.remove(os.path.join(backup_dir(), name))
        meta = os.path.join(backup_dir(), name[:-5] + ".json")
        if os.path.exists(meta):
            os.remove(meta)
        _audit(request, "backup.deleted", name)
        return Response(status=status.HTTP_204_NO_CONTENT)


class BackupDownloadView(_PlatformOnly):
    def get(self, request, name):
        self._existing(name)
        _audit(request, "backup.downloaded", name)
        return FileResponse(open(os.path.join(backup_dir(), name), "rb"), as_attachment=True, filename=name)


class BackupRestoreView(_PlatformOnly):
    def post(self, request, name):
        self._existing(name)
        if not _restore_enabled():
            return Response({"error": "Restore is switched off on this server (set ENABLE_BACKUP_RESTORE=true)."},
                            status=status.HTTP_403_FORBIDDEN)
        if str(request.data.get("confirm") or "") != f"RESTORE {name}":
            return Response({"error": f'Type "RESTORE {name}" to confirm. This replaces the live database.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if read_meta(name).get("status") not in ("completed", None):
            return Response({"error": "Only completed backups can be restored."}, status=status.HTTP_400_BAD_REQUEST)
        restore_database_backup.delay(name, request.user.email)
        _audit(request, "backup.restore_started", name)
        return Response({"name": name, "restore_status": "queued"}, status=status.HTTP_202_ACCEPTED)
