# grid_documents_interrogation/views.py
import csv
import logging
import os
import time
from io import StringIO

import pandas as pd
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from custom_authentication.permissions import IsClientOrAdminOrSuperUser

from .models import Topic, Query, DatabaseConnection
from .serializers import TopicSerializer, QuerySerializer, DatabaseConnectionSerializer
from core.models import File, Run
from core.utils import generate_and_register_service_report

from .tasks import process_file_query, process_db_query
from .db_query_tools import overview_query, test_connection
from .file_readers import read_pdf_file
from . import safety
from custom_authentication.policies import assistant_policy

HISTORY_TURNS = 20          # earlier question/answer pairs sent as memory
MAX_QUERY_CHARS = 8000


def _history(topic, user):
    """The last HISTORY_TURNS exchanges of a topic, oldest first (bounded, whatever the topic's size)."""
    recent = list(Query.objects.filter(topic=topic, user=user).order_by("-created_at")
                  .values_list("query_text", "response_text")[:HISTORY_TURNS])
    messages = []
    for q, a in reversed(recent):
        if isinstance(q, str) and q.strip():
            messages.append({"role": "user", "content": q})
        if isinstance(a, str) and a.strip():
            messages.append({"role": "assistant", "content": a})
    return messages


def _assistant_blocked(user):
    if not assistant_policy(user)["enabled"]:
        return Response({"error": "The Assistant is turned off for your organisation."}, status=403)
    return None
from core.access import get_file_for_user, get_scoped_or_404, get_run_for_user, scope_by_file, scope_by_owner

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Database connections
# ──────────────────────────────────────────────────────────────────────────────
class DatabaseConnectionViewSet(viewsets.ModelViewSet):
    serializer_class = DatabaseConnectionSerializer
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def get_queryset(self):
        return DatabaseConnection.objects.filter(owner=self.request.user)

    def perform_create(self, serializer):
        # Save and re-fetch to ensure encrypted fields (like password) are readable for URI building
        instance = serializer.save(owner=self.request.user)
        self._created_instance = DatabaseConnection.objects.get(pk=instance.pk)

    @action(detail=True, methods=["get"], url_path="test-connection")
    def test_connection_view(self, request, pk=None):
        db_conn = getattr(self, "_created_instance", None)
        if not db_conn or str(db_conn.pk) != pk:
            db_conn = get_object_or_404(DatabaseConnection, pk=pk, owner=request.user)

        try:
            url = safety.checked_url(db_conn.build_connection_uri())
            test_connection(url)
            return Response({"success": f"Connected to {safety.describe(url)}."})
        except safety.UnsafeTarget as e:
            return Response({"error": str(e)}, status=400)
        except Exception as e:
            logger.info("Saved connection %s test failed: %s", db_conn.pk, type(e).__name__)
            return Response({"error": "The database could not be reached with those details."}, status=400)


# ──────────────────────────────────────────────────────────────────────────────
# Topics
# ──────────────────────────────────────────────────────────────────────────────
class TopicViewSet(viewsets.ModelViewSet):
    serializer_class = TopicSerializer
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def get_queryset(self):
        return Topic.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['get'])
    def summary(self, request, pk=None):
        topic = self.get_object()
        files = topic.files.all()
        summary = {
            "id": topic.id,
            "project_id": topic.project_id,
            "service_id": topic.service_id,
            "chat_date": topic.chat_date.isoformat(),
            "files": [
                {
                    "file_id": f.id,
                    "file_name": f.filename,
                    "data_source": "File",
                    "created_at": f.created_at.isoformat(),
                    "status": f.status,
                }
                for f in files
            ]
        }
        return Response(summary)

    @action(detail=True, methods=['get'])
    def active_file(self, request, pk=None):
        topic = self.get_object()
        latest_file = topic.files.order_by('-created_at').first()
        if latest_file:
            return Response({"file_id": latest_file.id, "file_name": latest_file.filename})
        return Response({"active_file": None})

    @action(detail=True, methods=["post"])
    def attach_file(self, request, pk=None):
        topic = self.get_object()
        file_id = request.data.get("file_id")
        if not file_id:
            return Response({"error": "file_id is required"}, status=400)
        file_obj = get_file_for_user(request.user, file_id)
        topic.files.add(file_obj)
        return Response({"message": f"File {file_obj.filename} attached to topic {topic.id}."}, status=200)

    @action(detail=True, methods=["post"])
    def detach_file(self, request, pk=None):
        """Unlink a document from the topic (the document itself is untouched)."""
        topic = self.get_object()
        try:
            file_id = int(request.data.get("file_id"))
        except (TypeError, ValueError):
            return Response({"error": "file_id is required"}, status=400)
        topic.files.remove(file_id)
        return Response({"message": "Document removed from this topic."}, status=200)

    @action(detail=False, methods=['post'], url_path='start-interrogation')
    def start_interrogation(self, request):
        project_id = str(request.data.get("project_id") or "").strip()[:255]
        service_id = str(request.data.get("service_id") or "").strip()[:255]
        name = (str(request.data.get("name") or "").strip() or f"{project_id}-{service_id}")[:100]

        if not project_id or not service_id:
            return Response({"error": "project_id and service_id are required"}, status=400)

        topic, created = Topic.objects.get_or_create(
            user=request.user,
            project_id=project_id,
            service_id=service_id,
            defaults={"name": name}
        )

        return Response({
            "topic_id": topic.id,
            "project_id": topic.project_id,
            "service_id": topic.service_id,
            "created": created
        }, status=200)

    @action(detail=True, methods=["get"])
    def download_chats(self, request, pk=None):
        topic = self.get_object()
        queries = topic.queries.filter(user=request.user).order_by("created_at")

        history = []
        for q in queries:
            history.append({
                "query": q.query_text,
                "response": q.response_text,
                "file": q.file.filename if q.file else None,
                "timestamp": q.created_at.isoformat()
            })

        return Response({
            "topic_id": topic.id,
            "project_id": topic.project_id,
            "service_id": topic.service_id,
            "messages": history
        })


# ──────────────────────────────────────────────────────────────────────────────
# Queries
# ──────────────────────────────────────────────────────────────────────────────
class QueryViewSet(viewsets.ModelViewSet):
    """Questions are only created by ask/reask (which check access); no raw create or edit."""
    serializer_class = QuerySerializer
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]
    http_method_names = ["get", "post", "delete", "head", "options"]

    def create(self, request, *args, **kwargs):
        return Response({"error": "Use queries/ask/ to ask a question."}, status=405)

    def get_queryset(self):
        return Query.objects.filter(user=self.request.user).order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=False, methods=['post'], url_path='ask')
    def ask_query(self, request):
        """
        Body:
        {
          "topic_id": <int>,
          "query_text": "...",
          "data_source": "file" | "db",
          "file_id": <int>,                # when data_source="file"
          "connection_string": "...",      # optional; if omitted, uses Topic.db_connection
          "table": "users",                # for DB
          "llm_config": { "provider": "openai", "model": "gpt-4o-mini", ... },
          "project_id": optional (for report registration),
          "service_id": optional (for report registration),
          "generate_report": optional (default: false)
        }
        """
        start_time = time.time()
        topic_id = request.data.get("topic_id")
        query_text = request.data.get("query_text")
        data_source = (request.data.get("data_source") or "file").lower()
        file_id = request.data.get("file_id")

        # New parameters for report generation
        project_id = request.data.get("project_id")
        service_id = request.data.get("service_id")
        generate_report = request.data.get("generate_report", False)

        llm_config = safety.clean_llm_config(request.data.get("llm_config"))

        if not topic_id or not query_text:
            return Response({"error": "topic_id and query_text are required."}, status=400)
        blocked = _assistant_blocked(request.user)
        if blocked:
            return blocked
        query_text = str(query_text)[:MAX_QUERY_CHARS]

        topic = get_object_or_404(Topic, id=topic_id, user=request.user)

        if not topic.has_data_source and data_source != "db":
            return Response({
                "error": "No data source connected to this topic. Attach a file or link a database connection."
            }, status=400)

        previous_messages = _history(topic, request.user)

        try:
            file = None
            if data_source == "file":
                if not file_id:
                    return Response({"error": "file_id is required for file-based queries."}, status=400)
                file = get_file_for_user(request.user, file_id)

                # NEW: fast path (use cached text via file_id)
                task = process_file_query.delay(
                    query_text=query_text,
                    file_id=file.id,
                    llm_config=llm_config,
                    previous_messages=previous_messages
                )

            elif data_source == "db":
                if not safety.db_features_enabled():
                    return Response({"error": "Database questions are turned off on this platform."}, status=403)
                table_name = request.data.get("table")
                if not table_name:
                    return Response({"error": "Missing 'table' for DB query."}, status=400)
                try:
                    if request.data.get("connection_id") or request.data.get("connection_string"):
                        url = safety.url_for_request(request, request.data)
                    elif topic.db_connection and topic.db_connection.owner_id == request.user.id:
                        url = safety.checked_url(topic.db_connection.build_connection_uri())
                    else:
                        return Response({"error": "Choose a saved connection or enter a connection string."},
                                        status=400)
                except safety.UnsafeTarget as exc:
                    return Response({"error": str(exc)}, status=400)
                connection_string = url.render_as_string(hide_password=False)

                task = process_db_query.delay(
                    query_text,
                    connection_string,
                    table_name,
                    llm_config,
                    previous_messages=previous_messages
                )
            else:
                return Response({"error": "Invalid data_source. Must be 'file' or 'db'."}, status=400)

            # Synchronously wait for Celery result (kept same as your existing flow)
            result = task.get(timeout=60)
            execution_time = time.time() - start_time

            query = Query.objects.create(
                topic=topic,
                user=request.user,
                file=file,
                query_text=query_text,
                response_text=result
            )

            response_data = QuerySerializer(query).data
            response_data["execution_time_seconds"] = round(execution_time, 2)

            # Generate and register report if requested
            if generate_report and project_id and service_id:
                try:
                    run = Run.objects.create(
                        user=request.user,
                        project_id=project_id,
                        service_id=service_id,
                        status="completed",
                        result_json=response_data
                    )

                    report_info = generate_and_register_service_report(
                        service_name="Document Interrogation",
                        service_id="ai-document-interrogation",
                        vertical="AI Services",
                        response_data=response_data,
                        user=request.user,
                        run=run,
                        project_id=project_id,
                        service_id_folder=service_id,
                        folder_name="document-interrogation-results",
                        query=query_text,
                        execution_time_seconds=execution_time,
                        input_files=[{"filename": file.filename, "file_id": file.id}] if file else None,
                        additional_metadata={
                            "data_source": data_source,
                            "topic_id": topic_id,
                            "llm_model": llm_config.get("model", "unknown")
                        }
                    )
                    response_data["report_file"] = report_info
                    logger.info(f"✅ Generated interrogation report: {report_info.get('filename')}")
                except Exception as report_error:
                    logger.warning(f"Failed to generate report: {report_error}")
                    response_data["report_error"] = str(report_error)

            return Response(response_data, status=201)

        except Http404:
            raise
        except Exception:
            logger.exception("DocuChat question failed for topic %s", topic_id)
            return Response({"error": "The question could not be answered right now. Please try again."},
                            status=502)

    @action(detail=True, methods=['get'])
    def download(self, request, pk=None):
        query = self.get_object()
        queries = Query.objects.filter(topic=query.topic, user=request.user)

        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(['Filename', 'Query', 'Response', 'Project', 'Service'])

        for q in queries:
            writer.writerow([
                q.file.filename if q.file else '',
                q.query_text,
                q.response_text,
                q.topic.project_id,
                q.topic.service_id
            ])

        output.seek(0)
        response = Response(output.getvalue(), content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="chat_{pk}.csv"'
        return response

    @action(detail=True, methods=['delete'])
    def delete_chat(self, request, pk=None):
        query = self.get_object()
        file = query.file
        file_path = file.filepath if file else None

        # Only the chat message is removed. The document it referred to may be shared with,
        # or owned by, someone else, and is managed from the documents pages.
        query.delete()
        return Response({"success": "Message deleted."})

    @action(detail=False, methods=['get'], url_path='fetch-previous')
    def fetch_previous_chat(self, request):
        topic_id = request.query_params.get("topic_id")
        query_id = request.query_params.get("query_id")

        if not topic_id or not query_id:
            return Response({"error": "Missing topic_id or query_id"}, status=400)

        query = get_object_or_404(
            Query,
            id=query_id,
            topic_id=topic_id,
            user=request.user
        )

        return Response({
            "query_text": query.query_text,
            "response_text": query.response_text
        }, status=200)

    @action(detail=False, methods=['get'], url_path='recent')
    def recent_queries(self, request):
        queries = Query.objects.filter(user=request.user).order_by('-created_at')[:10]
        return Response(QuerySerializer(queries, many=True).data)

    @action(detail=True, methods=['post'], url_path='reask')
    def reask_query(self, request, pk=None):
        """
        Re-ask with history. Supports file-based follow-ups (fast path via file_id).
        """
        original = get_object_or_404(Query, id=pk, user=request.user)
        blocked = _assistant_blocked(request.user)
        if blocked:
            return blocked
        new_query_text = str(request.data.get("query_text") or original.query_text)[:MAX_QUERY_CHARS]
        llm_config = safety.clean_llm_config(request.data.get("llm_config"))
        previous_messages = _history(original.topic, request.user)

        if original.file:
            # Access may have been withdrawn since the first question.
            get_file_for_user(request.user, original.file_id)
            try:
                # NEW: route via Celery fast path with file_id
                task = process_file_query.delay(
                    query_text=new_query_text,
                    file_id=original.file.id,
                    llm_config=llm_config,
                    previous_messages=previous_messages
                )
                result = task.get(timeout=60)
            except Exception:
                logger.exception("DocuChat follow-up failed for query %s", pk)
                return Response({"error": "The follow-up could not be answered right now. Please try again."},
                                status=502)
        else:
            # Optional: add DB re-ask path using topic.db_connection if you want
            return Response({"error": "Re-ask is only supported for file-based queries for now."}, status=400)

        new_query = Query.objects.create(
            topic=original.topic,
            user=request.user,
            file=original.file,
            query_text=new_query_text,
            response_text=result
        )
        return Response(QuerySerializer(new_query).data)

    @action(detail=False, methods=['get'], url_path='chat-history')
    def chat_history(self, request):
        topic_id = request.query_params.get("topic_id")
        if not topic_id:
            return Response({"error": "Missing topic_id"}, status=400)

        topic = get_object_or_404(Topic, id=topic_id, user=request.user)
        queries = Query.objects.filter(topic=topic, user=request.user).order_by('created_at')
        serializer = QuerySerializer(queries, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='ocr-preview')
    def ocr_preview(self, request):
        file_id = request.query_params.get("file_id")
        if not file_id:
            return Response({"error": "file_id is required"}, status=400)

        file = get_file_for_user(request.user, file_id)
        try:
            text = read_pdf_file(file.filepath) if (file.filepath or "").lower().endswith(".pdf") else ""
            if not (text or "").strip():
                text = file.content or ""
            if not text.strip():
                return Response({"error": "No text could be read from this document."}, status=422)
            return Response({"ocr_text": text[:5000], "file_name": file.filename})
        except Exception as e:
            logger.info("OCR preview failed for file %s: %s", file.pk, type(e).__name__)
            return Response({"error": "No text could be read from this document."}, status=422)

    @action(detail=False, methods=['post'], url_path='db-inspect')
    def db_inspect(self, request):
        """
        Lightweight peek into a table.
        Body: { "connection_string": "...", "table": "users" }
        If connection_string omitted, will attempt to use Topic=db_connection if provided via ?topic_id=...
        """
        if not safety.db_features_enabled():
            return Response({"error": "Database questions are turned off on this platform."}, status=403)
        blocked = _assistant_blocked(request.user)
        if blocked:
            return blocked
        table_name = request.data.get("table")
        if not table_name:
            return Response({"error": "table is required"}, status=400)
        try:
            topic_id = request.query_params.get("topic_id") or request.data.get("topic_id")
            if not (request.data.get("connection_id") or request.data.get("connection_string")) and topic_id:
                topic = get_object_or_404(Topic, id=topic_id, user=request.user)
                if not topic.db_connection or topic.db_connection.owner_id != request.user.id:
                    return Response({"error": "This topic has no saved database connection."}, status=400)
                url = safety.checked_url(topic.db_connection.build_connection_uri())
            else:
                url = safety.url_for_request(request, request.data)
            cols, df = overview_query(url, table_name)
            return Response({
                "columns": cols,
                "sample_data": df.astype(str).to_dict(orient="records"),
                "database": safety.describe(url),
            })
        except safety.UnsafeTarget as e:
            return Response({"error": str(e)}, status=400)
        except Exception as e:
            logger.info("DocuChat db-inspect failed: %s", type(e).__name__)
            return Response({"error": "The database could not be reached with those details."}, status=400)
