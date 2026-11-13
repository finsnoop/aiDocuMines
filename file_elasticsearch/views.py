# file_elasticsearch/views.py

import time
import logging

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication
from custom_authentication.rbac import HasPermission
from custom_authentication.permissions import IsClientOrAdminOrSuperUser

from . import utils
from .serializers import SearchRequestSerializer, AdvancedSearchSerializer
from .tasks import reindex_files_task

from document_operations.utils import get_user_accessible_file_ids
from core.models import File, Run
from document_operations.models import FileAccessEntry
from core.utils import generate_and_register_service_report
from core.access import get_file_for_user, get_scoped_or_404, get_run_for_user, scope_by_file, scope_by_owner

logger = logging.getLogger(__name__)

class DeleteIndexView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [HasPermission('backup.delete')]

    def post(self, request):
        utils.delete_index()
        return Response({"message": "Index deleted."})

class ForceReindexView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [HasPermission('backup.create')]

    def post(self, request):
        reindex_files_task.delay()
        return Response({"message": "Reindex started."})

class IndexSingleFileView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, HasPermission('document.upload')]

    def post(self, request, file_id):
        try:
            file = get_file_for_user(request.user, file_id)
        except File.DoesNotExist:
            return Response({"error": "File not found"}, status=status.HTTP_404_NOT_FOUND)

        # allow if owner or explicitly shared (read is enough for indexing its text)
        is_owner = file.user_id == request.user.id
        is_shared = FileAccessEntry.objects.filter(file_id=file_id, user=request.user, can_read=True).exists()
        if not (is_owner or is_shared):
            return Response({"error": "Forbidden"}, status=status.HTTP_403_FORBIDDEN)

        utils.index_file(file)
        return Response({"message": f"File {file_id} indexed."})

class SearchView(APIView):
    """
    Elasticsearch ranked full-text search with pagination.
    POST /api/v1/es/search/
    body: {
        "query": "search term",
        "scope": "both" | "filename" | "content",
        "page": 1,
        "page_size": 50,
        "project_id": optional,
        "service_id": optional,
        "generate_report": false
    }
    Results are ranked by relevance (filename matches score highest).
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, HasPermission('search.semantic')]

    def post(self, request):
        start_time = time.time()
        serializer = SearchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        query = serializer.validated_data.get("query", "")
        scope = serializer.validated_data.get("scope", "both")
        page = serializer.validated_data.get("page", 1)
        page_size = serializer.validated_data.get("page_size", 50)
        project_id = request.data.get("project_id")
        service_id = request.data.get("service_id")
        generate_report = request.data.get("generate_report", False)

        accessible_ids = get_user_accessible_file_ids(request.user)
        result = utils.basic_search(
            query=query,
            scope=scope,
            user=request.user,
            accessible_ids=accessible_ids,
            page=page,
            page_size=page_size,
        )

        execution_time = time.time() - start_time
        response_data = {
            "results": result["results"],
            "total": result["total"],
            "page": result["page"],
            "page_size": result["page_size"],
            "max_score": result.get("max_score"),
            "query": query,
            "scope": scope,
            "execution_time_seconds": round(execution_time, 2),
        }

        if generate_report and project_id and service_id:
            try:
                run = Run.objects.create(
                    user=request.user,
                    project_id=project_id,
                    service_id=service_id,
                    status="completed",
                    result_json=response_data,
                )
                report_info = generate_and_register_service_report(
                    service_name="Elasticsearch Search",
                    service_id="ai-elasticsearch-search",
                    vertical="AI Services",
                    response_data=response_data,
                    user=request.user,
                    run=run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="elasticsearch-search-results",
                    query=query,
                    execution_time_seconds=execution_time,
                    additional_metadata={
                        "scope": scope,
                        "result_count": result["total"],
                    },
                )
                response_data["report_file"] = report_info
            except Exception as report_error:
                logger.warning("Failed to generate report: %s", report_error)
                response_data["report_error"] = str(report_error)

        return Response(response_data)


class AdvancedSearchView(APIView):
    """
    Elasticsearch advanced search with field-level filters and pagination.
    POST /api/v1/es/advanced-search/
    body: {
        "must": [{"field": "content", "value": "term"}, ...],
        "filter": [{"field": "status", "value": "Completed"}, ...],
        "page": 1,
        "page_size": 50,
        "project_id": optional,
        "service_id": optional,
        "generate_report": false
    }
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, HasPermission('search.advanced')]

    def post(self, request):
        start_time = time.time()
        serializer = AdvancedSearchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        project_id = request.data.get("project_id")
        service_id = request.data.get("service_id")
        generate_report = request.data.get("generate_report", False)
        page = serializer.validated_data.get("page", 1)
        page_size = serializer.validated_data.get("page_size", 50)

        accessible_ids = get_user_accessible_file_ids(request.user)
        result = utils.advanced_search(
            must=serializer.validated_data.get("must", []),
            filter_clauses=serializer.validated_data.get("filter", []),
            user=request.user,
            accessible_ids=accessible_ids,
            page=page,
            page_size=page_size,
        )

        execution_time = time.time() - start_time
        response_data = {
            "results": result["results"],
            "total": result["total"],
            "page": result["page"],
            "page_size": result["page_size"],
            "max_score": result.get("max_score"),
            "execution_time_seconds": round(execution_time, 2),
        }

        if generate_report and project_id and service_id:
            try:
                run = Run.objects.create(
                    user=request.user,
                    project_id=project_id,
                    service_id=service_id,
                    status="completed",
                    result_json=response_data,
                )
                report_info = generate_and_register_service_report(
                    service_name="Elasticsearch Advanced Search",
                    service_id="ai-elasticsearch-advanced-search",
                    vertical="AI Services",
                    response_data=response_data,
                    user=request.user,
                    run=run,
                    project_id=project_id,
                    service_id_folder=service_id,
                    folder_name="elasticsearch-advanced-search-results",
                    execution_time_seconds=execution_time,
                    additional_metadata={"result_count": result["total"]},
                )
                response_data["report_file"] = report_info
            except Exception as report_error:
                logger.warning("Failed to generate report: %s", report_error)
                response_data["report_error"] = str(report_error)

        return Response(response_data)

