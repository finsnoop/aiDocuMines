# system_settings/views.py

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django.shortcuts import get_object_or_404
from oauth2_provider.contrib.rest_framework import OAuth2Authentication, TokenHasReadWriteScope
from custom_authentication.permissions import IsClientOrAdminOrSuperUser
from oauth2_provider.models import Application
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from django.contrib.auth import get_user_model

from .models import SystemSettings, SystemSettingsAuditTrail
from .serializers import SystemSettingsSerializer
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

