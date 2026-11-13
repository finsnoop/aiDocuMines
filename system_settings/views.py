# system_settings/views.py
import logging
logger = logging.getLogger(__name__)

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django.shortcuts import get_object_or_404
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication
from custom_authentication.permissions import IsClientOrAdminOrSuperUser
from oauth2_provider.models import Application
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from django.contrib.auth import get_user_model

from .models import SystemSettings, SystemSettingsAuditTrail, PlatformGlobalSettings
from .serializers import SystemSettingsSerializer, PlatformGlobalSettingsSerializer
from .utils import get_default_settings_schema, get_default_settings_values

from custom_authentication.models import Client
from core.models import Run

User = get_user_model()

# Swagger param injection
client_id_param = openapi.Parameter("X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True)
client_secret_param = openapi.Parameter("X-Client-Secret", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True)

def get_user_from_client_id(client_id):
    try:
        app = Application.objects.get(client_id=client_id)
        return app.user
    except Application.DoesNotExist:
        return None

def get_client_from_user(user):
    try:
        return Client.objects.get(users=user)
    except Client.DoesNotExist:
        return None


class SystemSettingsView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(manual_parameters=[client_id_param, client_secret_param])
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        if not client_id:
            return Response({"error": "Missing client credentials"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client credentials"}, status=status.HTTP_401_UNAUTHORIZED)

        client = get_client_from_user(user)
        if not client:
            return Response({"error": "Client not found for user"}, status=status.HTTP_404_NOT_FOUND)

        settings, _ = SystemSettings.objects.get_or_create(client=client)
        serializer = SystemSettingsSerializer(settings)
        return Response(serializer.data)

    @swagger_auto_schema(manual_parameters=[client_id_param, client_secret_param])
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        if not client_id:
            return Response({"error": "Missing client credentials"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client credentials"}, status=status.HTTP_401_UNAUTHORIZED)

        client = get_client_from_user(user)
        if not client:
            return Response({"error": "Client not found for user"}, status=status.HTTP_404_NOT_FOUND)

        instance, _ = SystemSettings.objects.get_or_create(client=client)
        serializer = SystemSettingsSerializer(instance, data=request.data, partial=True)

        if serializer.is_valid():
            updated_settings = serializer.save()

            # Save audit trail
            SystemSettingsAuditTrail.objects.create(
                client=client,
                run=None,  # Optional: attach run_id if available
                changes=serializer.validated_data
            )

            return Response({"message": "Settings saved successfully", "settings": serializer.data})
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SystemSettingsResetView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(manual_parameters=[client_id_param, client_secret_param])
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        if not client_id:
            return Response({"error": "Missing client credentials"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client credentials"}, status=status.HTTP_401_UNAUTHORIZED)

        client = get_client_from_user(user)
        if not client:
            return Response({"error": "Client not found for user"}, status=status.HTTP_404_NOT_FOUND)

        defaults = get_default_settings_values()
        settings, _ = SystemSettings.objects.update_or_create(client=client, defaults=defaults)

        SystemSettingsAuditTrail.objects.create(
            client=client,
            run=None,
            changes={"reset_to_defaults": True}
        )

        return Response({
            "message": "Settings reset to default.",
            "settings": SystemSettingsSerializer(settings).data
        })


class SystemSettingsAuditTrailView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(manual_parameters=[client_id_param, client_secret_param])
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        if not client_id:
            return Response({"error": "Missing client credentials"}, status=status.HTTP_400_BAD_REQUEST)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client credentials"}, status=status.HTTP_401_UNAUTHORIZED)

        client = get_client_from_user(user)
        if not client:
            return Response({"error": "Client not found for user"}, status=status.HTTP_404_NOT_FOUND)

        audit_logs = SystemSettingsAuditTrail.objects.filter(client=client).order_by("-timestamp")[:25]
        data = [
            {
                "id": log.id,
                "changes": log.changes,
                "timestamp": log.timestamp,
                "run_id": log.run.run_id if log.run else None
            }
            for log in audit_logs
        ]
        return Response({"audit_trail": data})


class SystemSettingsSchemaView(APIView):
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def get(self, request):
        schema = get_default_settings_schema()
        return Response({"schema": schema})


# ============================================================================
# Platform Global Settings (superadmin only — singleton)
# ============================================================================

class PlatformGlobalSettingsView(APIView):
    """
    GET  → read the singleton row (creates it on first access)
    POST → update platform-wide defaults (superadmin only)
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    def get(self, request):
        # Only superadmins may read platform-global settings
        if not self._is_superadmin(request):
            return Response({"error": "SuperAdmin access required"}, status=status.HTTP_403_FORBIDDEN)

        settings_obj = PlatformGlobalSettings.load()
        serializer = PlatformGlobalSettingsSerializer(settings_obj)
        return Response(serializer.data)

    def post(self, request):
        if not self._is_superadmin(request):
            return Response({"error": "SuperAdmin access required"}, status=status.HTTP_403_FORBIDDEN)

        settings_obj = PlatformGlobalSettings.load()
        serializer = PlatformGlobalSettingsSerializer(settings_obj, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()

            # Platform-wide change: recorded in the admin audit log (the per-organisation
            # settings trail requires an organisation).
            try:
                from custom_authentication.models import AdminAuditEvent
                AdminAuditEvent.objects.create(
                    actor=request.user, client=getattr(request.user, "client", None),
                    action="platform_settings.updated", target_type="platform", target_label="Platform settings",
                    details={"changes": {k: str(v) for k, v in serializer.validated_data.items()}},
                )
            except Exception:
                logger.warning("Could not record platform settings change in the audit log", exc_info=True)
            return Response({"message": "Platform settings saved", "settings": serializer.data})
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @staticmethod
    def _is_superadmin(request) -> bool:
        """Platform-wide settings affect every organisation: only the platform team may
        read or change them, never an organisation's own SuperAdmin role."""
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return False
        from core.access import is_platform_superuser
        return is_platform_superuser(user)


class PlatformGlobalSettingsPublicView(APIView):
    """
    Public read-only endpoint — returns the platform-global LLM config
    so the frontend can bootstrap its default llm_config before the user
    has made a selection.

    No authentication required (safe: only exposes non-secret defaults).
    """
    permission_classes = []

    def get(self, request):
        settings_obj = PlatformGlobalSettings.load()
        return Response({
            "default_llm_provider": settings_obj.default_llm_provider,
            "default_llm_model": settings_obj.default_llm_model,
            "default_llm_temperature": settings_obj.default_llm_temperature,
            "default_llm_max_tokens": settings_obj.default_llm_max_tokens,
            "site_name": settings_obj.site_name,
        })

