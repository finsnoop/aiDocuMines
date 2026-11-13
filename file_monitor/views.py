from django.shortcuts import render
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
from django.shortcuts import get_object_or_404
from oauth2_provider.models import Application
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication
from django.http import FileResponse
import logging
from django.db.utils import IntegrityError
from django.http import JsonResponse
from .models import FileEventLog
from core.models import File
from core.views import get_user_from_client_id, file_id_param, client_id_param

logger = logging.getLogger(__name__)


class FileEventLogView(APIView):
    """
    ✅ Return a list of actions performed on a file (opened, modified, processed).
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="List file events (e.g. opened, modified, processed)",
        tags=["Core Application: File Events"],
        manual_parameters=[client_id_param, file_id_param],
        responses={200: "Success", 404: "Not Found"},
    )
    def get(self, request):
        client_id = request.headers.get("X-Client-ID")
        file_id = request.query_params.get("file_id")

        # ✅ Enforce OAuth2 authentication
        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        if not file_id:
            return Response({"error": "Missing file_id parameter"}, status=status.HTTP_400_BAD_REQUEST)

        file_instance = get_object_or_404(File, id=file_id)

        logs = FileEventLog.objects.filter(file=file_instance).order_by("-timestamp")
        data = [{
            "event_type": log.event_type,
            "timestamp": log.timestamp,
            "details": log.details,
            "triggered_by": log.triggered_by.email if log.triggered_by else "System"
        } for log in logs]

        return Response({
            "file_id": file_id,
            "filename": file_instance.filename,
            "events": data
        }, status=status.HTTP_200_OK)
