"""
Document Classification Views

API views for document clustering/classification with OAuth2 authentication.
"""

import logging
import uuid
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework import generics
from django.shortcuts import get_object_or_404
from django.http import JsonResponse
from oauth2_provider.models import Application
from oauth2_provider.contrib.rest_framework import OAuth2Authentication, TokenHasReadWriteScope
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from celery.result import AsyncResult

from core.models import File
from document_classification.models import ClusteringRun, ClusterResult, ClusterFile, SubClusterRun
from document_classification.serializers import (
    ClusteringRunSerializer,
    ClusteringRunListSerializer,
    ClusterResultSerializer,
    ClusterFileSerializer,
    ClusteringSubmitSerializer,
    SubClusterRunSerializer,
    SubClusterSubmitSerializer
)
from document_classification.tasks import cluster_documents_task, subcluster_documents_task
from document_classification.pagination import StandardResultsSetPagination

logger = logging.getLogger(__name__)

# Swagger Parameters
client_id_param = openapi.Parameter("X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True)
client_secret_param = openapi.Parameter("X-Client-Secret", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True)


def health_check(request):
    """Health check endpoint."""
    return JsonResponse({"status": "ok", "service": "document_classification"}, status=200)


def get_user_from_client_id(client_id):
    """Get user from OAuth2 client ID."""
    try:
        application = Application.objects.get(client_id=client_id)
        return application.user
    except Application.DoesNotExist:
        return None


def get_client_from_user(user):
    """Get Client model from user."""
    try:
        from custom_authentication.models import Client
        return Client.objects.filter(user=user).first()
    except Exception:
        return None


class SubmitClusteringAPIView(APIView):
    """
    Submit documents for clustering/classification.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        manual_parameters=[client_id_param, client_secret_param],
        request_body=ClusteringSubmitSerializer,
        operation_description="Submit documents for clustering. Requires at least 2 files."
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")
        
        if not all([client_id, client_secret]):
            return Response({"error": "Missing client credentials"}, status=status.HTTP_400_BAD_REQUEST)
        
        application = get_object_or_404(Application, client_id=client_id)
        user = application.user
        client = get_client_from_user(user)
        
        # Validate input
        serializer = ClusteringSubmitSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        file_ids = data['file_ids']
        project_id = data['project_id']
        service_id = data['service_id']
        
        # Verify files belong to user
        from core.access import get_files_for_user
        files = [f for f in get_files_for_user(request.user, file_ids, "process")
                 if f.project_id == project_id and f.service_id == service_id]
        
        for file_obj in files:
            if str(file_obj.user.id) != str(user.id):
                raise PermissionDenied(f"You are not authorized to process file {file_obj.id}")
        
        if len(files) < 2:
            return Response(
                {"error": "At least 2 valid files are required for clustering"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Create clustering run
        run = ClusteringRun.objects.create(
            id=uuid.uuid4(),
            client=client,
            user=user,
            project_id=project_id,
            service_id=service_id,
            client_name=user.username or user.email,
            clustering_method=data.get('clustering_method', 'agglomerative'),
            nb_cluster_method=data.get('nb_cluster_method', 'silhouette'),
            embedding_model=data.get('embedding_model', 'bert-base-uncased'),
            generate_descriptions=data.get('generate_descriptions', True),
            status='Pending'
        )
        
        # Start async task
        task = cluster_documents_task.delay(
            run_id=str(run.id),
            file_ids=list(file_ids),
            clustering_method=run.clustering_method,
            nb_cluster_method=run.nb_cluster_method,
            embedding_model=run.embedding_model,
            generate_descriptions=run.generate_descriptions
        )
        
        return Response({
            "run_id": str(run.id),
            "task_id": task.id,
            "status": "Pending",
            "file_count": len(files),
            "message": "Clustering started"
        }, status=status.HTTP_202_ACCEPTED)


class ClusteringStatusAPIView(APIView):
    """
    Get the status of a clustering run.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        manual_parameters=[
            client_id_param,
            openapi.Parameter("run_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True)
        ]
    )
    def get(self, request):
        run_id = request.query_params.get("run_id")
        if not run_id:
            return Response({"error": "run_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        client_id = request.headers.get("X-Client-ID")
        user = get_user_from_client_id(client_id)

        run = get_object_or_404(ClusteringRun, id=run_id)

        # Verify ownership
        if run.user and str(run.user.id) != str(user.id):
            raise PermissionDenied("You are not authorized to view this run")

        file_count = run.cluster_files.count()
        completed_files = run.cluster_files.filter(status='Completed').count()
        failed_files = run.cluster_files.filter(status='Failed').count()

        return Response({
            "run_id": str(run.id),
            "status": run.status,
            "optimal_clusters": run.optimal_clusters,
            "file_count": file_count,
            "completed_files": completed_files,
            "failed_files": failed_files,
            "error_message": run.error_message,
            "created_at": run.created_at,
            "updated_at": run.updated_at
        })


class ClusteringResultsAPIView(APIView):
    """
    Get the full results of a completed clustering run.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        manual_parameters=[
            client_id_param,
            openapi.Parameter("run_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True)
        ]
    )
    def get(self, request):
        run_id = request.query_params.get("run_id")
        if not run_id:
            return Response({"error": "run_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        client_id = request.headers.get("X-Client-ID")
        user = get_user_from_client_id(client_id)

        run = get_object_or_404(ClusteringRun, id=run_id)

        # Verify ownership
        if run.user and str(run.user.id) != str(user.id):
            raise PermissionDenied("You are not authorized to view this run")

        serializer = ClusteringRunSerializer(run)
        return Response(serializer.data)


class ClusteringRunListAPIView(generics.ListAPIView):
    """
    List all clustering runs for the authenticated user.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]
    serializer_class = ClusteringRunListSerializer
    pagination_class = StandardResultsSetPagination

    @swagger_auto_schema(
        manual_parameters=[
            client_id_param,
            openapi.Parameter("project_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter("service_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
            openapi.Parameter("status", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False),
        ]
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        client_id = self.request.headers.get("X-Client-ID")
        user = get_user_from_client_id(client_id)

        qs = ClusteringRun.objects.filter(user=user).order_by('-created_at')

        project_id = self.request.query_params.get("project_id")
        service_id = self.request.query_params.get("service_id")
        status_filter = self.request.query_params.get("status")

        if project_id:
            qs = qs.filter(project_id=project_id)
        if service_id:
            qs = qs.filter(service_id=service_id)
        if status_filter:
            qs = qs.filter(status=status_filter)

        return qs


class ClusterDetailsAPIView(APIView):
    """
    Get details of a specific cluster including its files.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        manual_parameters=[
            client_id_param,
            openapi.Parameter("run_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True),
            openapi.Parameter("cluster_id", openapi.IN_QUERY, type=openapi.TYPE_INTEGER, required=True)
        ]
    )
    def get(self, request):
        run_id = request.query_params.get("run_id")
        cluster_id = request.query_params.get("cluster_id")

        if not run_id or cluster_id is None:
            return Response({"error": "run_id and cluster_id are required"}, status=status.HTTP_400_BAD_REQUEST)

        client_id = request.headers.get("X-Client-ID")
        user = get_user_from_client_id(client_id)

        run = get_object_or_404(ClusteringRun, id=run_id)

        if run.user and str(run.user.id) != str(user.id):
            raise PermissionDenied("You are not authorized to view this run")

        cluster_result = get_object_or_404(ClusterResult, run=run, cluster_id=int(cluster_id))
        serializer = ClusterResultSerializer(cluster_result)

        return Response(serializer.data)


class SubClusterAPIView(APIView):
    """
    Submit sub-clustering on an existing cluster.
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        manual_parameters=[client_id_param, client_secret_param],
        request_body=SubClusterSubmitSerializer,
        operation_description="Sub-cluster files within an existing cluster."
    )
    def post(self, request):
        client_id = request.headers.get("X-Client-ID")
        client_secret = request.headers.get("X-Client-Secret")

        if not all([client_id, client_secret]):
            return Response({"error": "Missing client credentials"}, status=status.HTTP_400_BAD_REQUEST)

        application = get_object_or_404(Application, client_id=client_id)
        user = application.user

        serializer = SubClusterSubmitSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        run_id = data['run_id']
        cluster_id = data['cluster_id']

        # Verify run exists and belongs to user
        run = get_object_or_404(ClusteringRun, id=run_id)
        if run.user and str(run.user.id) != str(user.id):
            raise PermissionDenied("You are not authorized to sub-cluster this run")

        # Create sub-cluster run
        sub_run = SubClusterRun.objects.create(
            parent_run=run,
            parent_cluster_id=cluster_id,
            status='Pending'
        )

        task = subcluster_documents_task.delay(
            sub_run_id=str(sub_run.id),
            run_id=str(run_id),
            cluster_id=cluster_id,
            clustering_method=data.get('clustering_method', 'agglomerative'),
            nb_cluster_method=data.get('nb_cluster_method', 'silhouette')
        )

        return Response({
            "sub_run_id": str(sub_run.id),
            "task_id": task.id,
            "status": "Pending",
            "parent_run_id": str(run_id),
            "parent_cluster_id": cluster_id,
            "message": "Sub-clustering started"
        }, status=status.HTTP_202_ACCEPTED)

    @swagger_auto_schema(
        manual_parameters=[
            client_id_param,
            openapi.Parameter("sub_run_id", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=True)
        ]
    )
    def get(self, request):
        sub_run_id = request.query_params.get("sub_run_id")
        if not sub_run_id:
            return Response({"error": "sub_run_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        client_id = request.headers.get("X-Client-ID")
        user = get_user_from_client_id(client_id)

        sub_run = get_object_or_404(SubClusterRun, id=sub_run_id)

        if sub_run.parent_run.user and str(sub_run.parent_run.user.id) != str(user.id):
            raise PermissionDenied("You are not authorized to view this sub-cluster run")

        serializer = SubClusterRunSerializer(sub_run)
        return Response(serializer.data)

