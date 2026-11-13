# app_layout/task_statuses.py

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from .models import TaskStatus, UploadedDocument
from .serializers import TaskStatusSerializer
from django.shortcuts import get_object_or_404
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi

# ------------------------------------------
# Swagger parameters
# ------------------------------------------
document_id_param = openapi.Parameter(
    "document_id", openapi.IN_PATH, description="Document ID", type=openapi.TYPE_INTEGER
)

class TaskStatusView(APIView):
    """
    ✅ Retrieve the most recent task status for a given document
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Retrieve the latest task status for a document.",
        tags=["Task Status"],
        manual_parameters=[document_id_param],
        responses={200: TaskStatusSerializer, 404: "Not Found"}
    )
    def get(self, request, document_id):
        document = get_object_or_404(UploadedDocument, id=document_id)
        task_status = TaskStatus.objects.filter(document=document).order_by('-created_at').first()

        if not task_status:
            return Response({"status": "unknown"}, status=status.HTTP_404_NOT_FOUND)

        serializer = TaskStatusSerializer(task_status)
        return Response(serializer.data, status=status.HTTP_200_OK)


class AllTaskStatusesView(APIView):
    """
    ✅ Retrieve all task statuses for the logged-in user
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Retrieve all task statuses for the authenticated user.",
        tags=["Task Status"],
        responses={200: TaskStatusSerializer(many=True)}
    )
    def get(self, request):
        user = request.user
        documents = UploadedDocument.objects.filter(user=user)
        statuses = TaskStatus.objects.filter(document__in=documents).order_by('-created_at')
        serializer = TaskStatusSerializer(statuses, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

