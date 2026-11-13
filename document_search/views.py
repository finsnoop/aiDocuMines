# document_search/views.py

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, generics
from rest_framework.permissions import IsAuthenticated
from django.http import Http404
from document_search import milvus_store

from oauth2_provider.contrib.rest_framework import OAuth2Authentication
from custom_authentication.rbac import HasPermission
from custom_authentication.permissions import IsClientOrAdminOrSuperUser

from django.core.cache import cache
from celery.result import AsyncResult

from document_search.models import VectorChunk
from document_search.serializers import (
    SearchRequestSerializer,
    SearchResultSerializer,
    VectorChunkSerializer,
    IndexRequestSerializer,
    AsyncSearchResponse
)
from core.models import File
from document_search.utils import _get_model

from document_search.tasks import (
    index_file,
    bulk_reindex,
    _ensure_collection,
)
from document_search.tasks import semantic_search_task

import time
from django.db.models import Q
from datetime import datetime
import logging

# 🔁 Use the same config the tasks use (host/port/collection)
from document_search.config import (
    COLLECTION_NAME,
    MILVUS_HOST,
    MILVUS_PORT,
    MILVUS_METRIC_TYPE,
    MILVUS_NPROBE,
    MILVUS_EF,
    MILVUS_SEARCH_TOP_K,
)

from document_operations.utils import get_user_accessible_file_ids
from document_operations.models import FileAccessEntry
from core.access import get_file_for_user, get_scoped_or_404, get_run_for_user, scope_by_file, scope_by_owner

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 🧠  Connect to Milvus collection (same host/port as tasks.py)
# ─────────────────────────────────────────────────────────────
# -------------------------------------------------------------
# Semantic File Search
# -------------------------------------------------------------
class SemanticFileSearchView(APIView):
    """
    Search using semantic embeddings (vector search in Milvus).
    POST /api/v1/document-search/semantic-search/
    body: {
        "query": "data science",
        "top_k": 5,
        "file_id": optional,
        "filters": optional,
        "project_id": optional (for report registration),
        "service_id": optional (for report registration),
        "generate_report": optional (default: false)
    }
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, HasPermission('search.semantic')]

    def post(self, request, *args, **kwargs):
        user = request.user
        query = request.data.get("query")
        top_k = int(request.data.get("top_k", 5))
        file_id = request.data.get("file_id")
        filters = request.data.get("filters", {})

        # New parameters for report generation
        project_id = request.data.get("project_id")
        service_id = request.data.get("service_id")
        generate_report = request.data.get("generate_report", False)

        if not query:
            return Response({"error": "Query is required"}, status=400)

        # ✅ Determine file scope: only owned/shared files
        accessible_file_ids = get_user_accessible_file_ids(user)

        # Optional narrowing to single file — enforce access
        if file_id and int(file_id) not in accessible_file_ids:
            return Response({"error": "You do not have access to this file."}, status=403)

        # 🚀 Enqueue Celery task with kwargs (prevents positional arg mismatches)
        task = semantic_search_task.apply_async(kwargs={
            "user_id": user.id,
            "query": query,
            "top_k": top_k,
            "file_id": file_id,
            "filters": filters,
            "project_id": project_id,
            "service_id": service_id,
            "generate_report": generate_report,
        })

        # ⬇️ add this line
        cache.set(f"task_owner:{task.id}", request.user.id, timeout=3600)

        return Response({"task_id": task.id}, status=status.HTTP_202_ACCEPTED)


# ─────────────────────────────────────────────────────────────
# 🔍 Search API (sync)
# ─────────────────────────────────────────────────────────────
class ChunkedFileSearchView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, HasPermission('search.semantic')]

    def post(self, request):
        serializer = SearchRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        query = serializer.validated_data["query"]
        file_id = serializer.validated_data.get("file_id")
        top_k = serializer.validated_data.get("top_k", 10)
        page = serializer.validated_data.get("page", 1)
        page_size = serializer.validated_data.get("page_size", 10)

        accessible_file_ids = set(get_user_accessible_file_ids(user))

        if file_id and int(file_id) not in accessible_file_ids:
            return Response({"error": "You do not have access to this file."}, status=403)
        if file_id:
            accessible_file_ids &= {int(file_id)}
        if not accessible_file_ids:
            return Response({"results": [], "total": 0, "page": page}, status=200)

        embed_model = _get_model()
        query_vector = embed_model.encode([query])[0]

        client_id_val = user.client_id if getattr(user, "client_id", None) else -1
        offset = (page - 1) * page_size
        fetch_top_k = offset + page_size

        try:
            results = [milvus_store.search(query_vector, accessible_file_ids, client_id_val, max(fetch_top_k, top_k))]
        except Exception:
            logger.exception("Semantic search failed")
            return Response({"error": "Semantic search is temporarily unavailable. Please try again shortly."},
                            status=503)

        seen_file_ids = set()
        all_matches = []
        for hit in results[0]:
            fid = int(hit.entity.get("file_id"))
            if fid in seen_file_ids:
                continue
            seen_file_ids.add(fid)
            if fid not in accessible_file_ids:
                continue
            try:
                file_obj = get_file_for_user(request.user, fid)
            except (File.DoesNotExist, Http404):
                continue
            all_matches.append({
                "file_id": fid,
                "file_name": getattr(file_obj, "filename", str(file_obj)),
                "chunk_text": hit.entity.get("chunk_text", ""),
                "score": float(hit.score),
            })

        page_matches = all_matches[offset:offset + page_size] if offset < len(all_matches) else []
        return Response({
            "results": page_matches,
            "total": len(all_matches),
            "page": page,
            "page_size": page_size,
            "query": query,
        }, status=200)


class VectorChunkListView(generics.ListAPIView):
    serializer_class = VectorChunkSerializer
    permission_classes = [HasPermission('search.advanced')]

    def get_queryset(self):
        user = self.request.user
        qs = VectorChunk.objects.select_related("file")
        if user.is_superuser:
            return qs
        if hasattr(user, "client") and user.client:
            return qs.filter(file__user__client=user.client)
        return qs.filter(file__user=user)


class TriggerVectorIndexingView(APIView):
    """
    POST  /api/v1/document-search/index/
    body: {"file_ids": [2, 3, 4], "force": false}
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, HasPermission('document.upload')]

    def post(self, request):
        ser = IndexRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        file_ids = ser.validated_data["file_ids"]
        force = ser.validated_data["force"]

        # ✅ Restrict to accessible file IDs
        allowed_ids = set(get_user_accessible_file_ids(request.user))
        filtered_ids = [fid for fid in file_ids if fid in allowed_ids]

        if not filtered_ids:
            return Response({"error": "No valid files to index."}, status=403)

        for fid in filtered_ids:
            index_file.delay(fid, force=force)

        return Response(
            {
                "queued": len(filtered_ids),
                "force": force,
                "file_ids": filtered_ids,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class BulkReindexMissingView(APIView):
    """
    POST  /api/v1/document-search/reindex-missing/
    – queues indexing for every file that currently has **no** VectorChunk rows.
    """
    permission_classes = [HasPermission('search.advanced')]

    def post(self, request):
        task = bulk_reindex.delay()
        cache.set(f"task_owner:{task.id}", request.user.id, timeout=3600)
        return Response(
            {"message": "Bulk re-index started", "task_id": task.id},
            status=status.HTTP_202_ACCEPTED,
        )


class AdvancedDocumentSearchView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [HasPermission('search.advanced')]

    def post(self, request):
        user = request.user
        query = request.data.get("query")
        top_k = int(request.data.get("top_k", 10))
        filters = request.data.get("filters", {})

        if not query:
            return Response({"error": "Query is required"}, status=400)

        # Step 1: resolve accessible files first
        accessible_file_ids = set(get_user_accessible_file_ids(user))
        if not accessible_file_ids:
            return Response({"count": 0, "results": []}, status=200)

        # Step 2-3: vector search scoped to accessible files + organisation
        embed_model = _get_model()
        query_vector = embed_model.encode([query])[0]
        client_id_val = user.client_id if getattr(user, "client_id", None) else -1
        try:
            results = [milvus_store.search(query_vector, accessible_file_ids, client_id_val, top_k)]
        except Exception:
            logger.exception("Semantic search failed")
            return Response({"error": "Semantic search is temporarily unavailable. Please try again shortly."},
                            status=503)

        vector_file_ids = {int(hit.entity.get("file_id")) for hit in results[0]}
        allowed_file_ids = vector_file_ids & accessible_file_ids

        if not allowed_file_ids:
            return Response({"count": 0, "results": []}, status=200)

        # Step 4: apply additional Django-level filters
        q = Q(id__in=allowed_file_ids)
        if filters.get("created_from"):
            q &= Q(created_at__gte=datetime.fromisoformat(filters["created_from"]))
        if filters.get("created_to"):
            q &= Q(created_at__lte=datetime.fromisoformat(filters["created_to"]))
        if filters.get("author"):
            q &= Q(metadata__author__icontains=filters["author"])
        if filters.get("project_id"):
            q &= Q(project_id=filters["project_id"])
        if filters.get("service_id"):
            q &= Q(service_id=filters["service_id"])

        files = File.objects.filter(q).prefetch_related("metadata")

        best_per_file = {}
        for hit in results[0]:
            fid = int(hit.entity.get("file_id"))
            if fid not in allowed_file_ids:
                continue
            score = float(hit.score)
            chunk = hit.entity.get("chunk_text", "")
            if (fid not in best_per_file) or (score > best_per_file[fid][1]):
                best_per_file[fid] = (chunk, score)

        results_out = []
        for file in files:
            metadata = file.metadata.first()
            chunk_text, score = best_per_file.get(file.id, ("", None))
            results_out.append({
                "file_id": file.id,
                "filename": file.filename,
                "file_size": file.file_size,
                "file_type": file.file_type,
                "created_at": file.created_at,
                "author": metadata.author if metadata else None,
                "keywords": metadata.keywords if metadata else None,
                "chunk_text": chunk_text,
                "score": score,
            })

        return Response({
            "count": len(results_out),
            "results": results_out
        }, status=200)


class SearchResultView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [IsAuthenticated, HasPermission('search.semantic')]

    def get(self, request, task_id, *args, **kwargs):
        task_id = str(task_id)

        # ✅ verify caller owns this task_id
        owner_id = cache.get(f"task_owner:{task_id}")
        if owner_id is None or owner_id != request.user.id:
            # Hide existence to avoid leaking task IDs
            return Response({"status": "error", "error": "Not found"}, status=404)

        result = AsyncResult(task_id)

        if result.state == "PENDING":
            return Response({"status": "pending"}, status=202)

        elif result.state == "FAILURE":
            return Response({
                "status": "error",
                "error": str(result.result),
            }, status=500)

        elif result.state == "SUCCESS":
            return Response({
                "status": "ok",
                "results": result.result,
                "count": len(result.result) if result.result else 0,
            })

        else:
            return Response({"status": result.state})


class CheckIndexView(APIView):
    """
    POST /api/v1/document-search/check-index/
    Body: {"file_ids": [1, 2, 3]}
    Returns vector indexing status for each file.
    Only returns status for files the user can access.
    """
    permission_classes = [HasPermission('search.semantic')]

    def post(self, request):
        file_ids = request.data.get("file_ids", [])
        if not file_ids:
            return Response({"error": "file_ids is required"}, status=400)

        accessible_ids = set(get_user_accessible_file_ids(request.user))
        results = {}
        for fid in file_ids:
            if int(fid) not in accessible_ids:
                continue
            has_chunks = VectorChunk.objects.filter(file_id=fid).exists()
            results[fid] = {"indexed": has_chunks}

        return Response({"results": results}, status=200)

