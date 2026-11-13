# grid_documents_interrogation/views.py
import csv
import logging
import os
import time
from io import StringIO

import pandas as pd
from django.db.models import Max, OuterRef, Q, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from custom_authentication.permissions import IsClientOrAdminOrSuperUser

from .models import Topic, Query, DatabaseConnection
from .serializers import TopicSerializer, QuerySerializer, DatabaseConnectionSerializer
from core.models import File, Run
from core.utils import generate_and_register_service_report

from document_operations.utils import has_file_access
from .tasks import process_file_query, process_db_query
from .utils import fit_messages_to_context
from .db_query_tools import fetch_column_names, generate_sql_query, execute_sql_query, test_connection, preview_table, scrub_db_error
from .file_readers import read_file
from . import safety
from custom_authentication.policies import assistant_policy

logger = logging.getLogger(__name__)

HISTORY_TURNS = 20          # earlier question/answer pairs loaded per chat (then fitted to the model's context)
MAX_QUERY_CHARS = 8000


def _assistant_blocked(user):
    if not assistant_policy(user)["enabled"]:
        return Response({"error": "The Assistant is turned off for your organisation."}, status=403)
    return None


def _owned_connection_url(request, connection_id=None, topic=None):
    """Vetted URL of one of the caller's saved connections (by id, else the topic's), or raise UnsafeTarget."""
    conn_qs = DatabaseConnection.objects.filter(owner=request.user)
    client_id = getattr(request.user, "client_id", None)
    if client_id:
        conn_qs = conn_qs.filter(owner__client_id=client_id)
    db_conn = None
    if connection_id:
        db_conn = conn_qs.filter(pk=connection_id).first()
    elif topic is not None and topic.db_connection_id:
        db_conn = conn_qs.filter(pk=topic.db_connection_id).first()
    if not db_conn:
        raise safety.UnsafeTarget("Choose one of your saved database connections.")
    return db_conn, safety.checked_url(db_conn.build_connection_uri())


class TopicPagination(PageNumberPagination):
    page_size = 8
    page_size_query_param = "page_size"
    max_page_size = 50


def _file_for_chat(user, file_id):
    file = get_object_or_404(File, id=file_id)
    if not has_file_access(user, file, "read"):
        raise NotFound("File not found")
    return file


def _visible_topic_files(topic, user):
    files = [f for f in topic.files.all() if has_file_access(user, f, "read")]
    files.sort(key=lambda f: (f.created_at, f.id))
    return files


def _extract_owned_file_text(file):
    text = (file.content or "").strip()
    if len(text) >= 40 or not file.filepath:
        return text
    try:
        extracted = read_file(file.filepath)
        if isinstance(extracted, tuple):
            extracted = extracted[0]
        if extracted:
            return str(extracted).strip()
    except Exception:
        return text
    return text


def _messages_for_topic(topic, user):
    messages = []
    recent = list(Query.objects.filter(topic=topic, user=user).order_by("-created_at")
                  .values_list("query_text", "response_text")[:HISTORY_TURNS])
    for q, a in reversed(recent):
        if isinstance(q, str) and q.strip():
            messages.append({"role": "user", "content": q})
        if isinstance(a, str) and a.strip():
            messages.append({"role": "assistant", "content": a})
    return messages


def _build_previous_messages(topic, user, llm_config, request):
    """Own chat only, unless this request explicitly opts into other chats."""
    own = _messages_for_topic(topic, user)
    extra = []
    if request.data.get("reference_other_chats") is True:
        raw_ids = request.data.get("reference_topic_ids") or []
        ids = []
        for item in raw_ids:
            try:
                tid = int(item)
            except (TypeError, ValueError):
                continue
            if tid != topic.id and tid not in ids:
                ids.append(tid)
        if ids:
            others = Topic.objects.filter(id__in=ids[:20], user=user).exclude(id=topic.id)
            for other in others:
                label = other.name or f"Chat {other.id}"
                extra.append({"role": "user", "content": f"[Referenced chat only, not this document: {label}]"})
                extra.extend(_messages_for_topic(other, user))
    return fit_messages_to_context(extra + own, (llm_config or {}).get("model"))


# ──────────────────────────────────────────────────────────────────────────────
# Database connections
# ──────────────────────────────────────────────────────────────────────────────
class DatabaseConnectionViewSet(viewsets.ModelViewSet):
    serializer_class = DatabaseConnectionSerializer
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]

    def get_queryset(self):
        qs = DatabaseConnection.objects.filter(owner=self.request.user)
        client_id = getattr(self.request.user, "client_id", None)
        if client_id:
            qs = qs.filter(owner__client_id=client_id)
        return qs

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
    pagination_class = TopicPagination

    def get_queryset(self):
        latest = Query.objects.filter(topic=OuterRef("pk"), user=OuterRef("user")).order_by("-created_at")
        qs = Topic.objects.filter(user=self.request.user).annotate(
            last_activity=Max("queries__created_at"),
            latest_query_text=Subquery(latest.values("query_text")[:1]),
        )
        q = (self.request.query_params.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(project_id__icontains=q)
                | Q(service_id__icontains=q)
                | Q(queries__query_text__icontains=q)
                | Q(queries__response_text__icontains=q)
            ).distinct()
        return qs.order_by("-last_activity", "-chat_date", "-id")

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['get'])
    def summary(self, request, pk=None):
        topic = self.get_object()
        files = _visible_topic_files(topic, request.user)
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
        visible = _visible_topic_files(topic, request.user)
        latest_file = visible[-1] if visible else None
        if latest_file:
            return Response({"file_id": latest_file.id, "file_name": latest_file.filename})
        return Response({"active_file": None})

    @action(detail=True, methods=["post"])
    def attach_file(self, request, pk=None):
        topic = self.get_object()
        file_id = request.data.get("file_id")
        if not file_id:
            return Response({"error": "file_id is required"}, status=400)
        file_obj = _file_for_chat(request.user, file_id)
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

        if request.data.get("force_new"):
            topic = Topic.objects.create(
                user=request.user,
                project_id=project_id,
                service_id=service_id,
                name=name,
            )
            created = True
        else:
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
                "file": q.file.filename if q.file and has_file_access(request.user, q.file, "read") else None,
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
        topic = serializer.validated_data.get("topic")
        file = serializer.validated_data.get("file")
        if topic is None or topic.user_id != self.request.user.id:
            raise NotFound("Topic not found")
        if file is not None and not has_file_access(self.request.user, file, "read"):
            raise NotFound("File not found")
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
          "connection_id": <int>,          # a saved connection; if omitted, uses Topic.db_connection
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

        llm_config = safety.clean_llm_config(request.data.get("llm_config"), request.user)

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

        previous_messages = _build_previous_messages(topic, request.user, llm_config, request)

        try:
            file = None
            if data_source == "file":
                if not file_id:
                    return Response({"error": "file_id is required for file-based queries."}, status=400)
                file = _file_for_chat(request.user, file_id)

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
                if request.data.get("connection_string"):
                    return Response(
                        {"error": "Raw connection strings are not accepted. Select a saved connection."},
                        status=400,
                    )
                table_name = request.data.get("table")
                if not table_name:
                    return Response({"error": "Missing 'table' for DB query."}, status=400)
                try:
                    _conn, url = _owned_connection_url(request, request.data.get("connection_id"), topic)
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

        except (Http404, NotFound):
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
        # Only the chat message is removed. The document it referred to (yours, or shared with
        # you) is managed from the documents pages and is never deleted from a chat.
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
        llm_config = safety.clean_llm_config(request.data.get("llm_config"), request.user)

        previous_messages = _build_previous_messages(
            original.topic, request.user, llm_config, request
        )

        source_file = original.file or original.topic.files.order_by("-created_at").first()
        if source_file and not has_file_access(request.user, source_file, "read"):
            return Response({"error": "File not found"}, status=404)
        if source_file:
            try:
                # NEW: route via Celery fast path with file_id
                task = process_file_query.delay(
                    query_text=new_query_text,
                    file_id=source_file.id,
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
            file=source_file,
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

        file = _file_for_chat(request.user, file_id)
        text = _extract_owned_file_text(file)
        ready = len(text) >= 40
        excerpt = " ".join(text.split())[:700]
        return Response({
            "filename": file.filename,
            "file_id": file.id,
            "project_id": file.project_id,
            "service_id": file.service_id,
            "ready": ready,
            "chars": len(text),
            "excerpt": excerpt,
            "ocr_text": excerpt,
            "hint": (
                "This document has readable text. Ask a question or use a preset."
                if ready else
                "No readable text yet. Use Run OCR to extract it."
            ),
        })

    @action(detail=False, methods=["post"], url_path="prepare-document")
    def prepare_document(self, request):
        file_id = request.data.get("file_id")
        if not file_id:
            return Response({"error": "file_id is required"}, status=400)
        file = _file_for_chat(request.user, file_id)
        can_write = file.user_id == request.user.id or has_file_access(request.user, file, "write")
        text = _extract_owned_file_text(file)
        if len(text) >= 40:
            if can_write and (file.content or "").strip() != text:
                file.content = text[:200000]
                file.save(update_fields=["content"])
            return Response({
                "ready": True,
                "queued": False,
                "filename": file.filename,
                "file_id": file.id,
                "project_id": file.project_id,
                "service_id": file.service_id,
                "chars": len(text),
                "excerpt": " ".join(text.split())[:700],
            })
        ext = (file.extension or file.filename.rsplit(".", 1)[-1]).lower()
        if not can_write:
            return Response({
                "ready": False,
                "queued": False,
                "filename": file.filename,
                "file_id": file.id,
                "hint": "This shared document has no readable text yet.",
            })
        if ext not in {"pdf", "png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"}:
            return Response({
                "ready": False,
                "queued": False,
                "filename": file.filename,
                "file_id": file.id,
                "project_id": file.project_id,
                "service_id": file.service_id,
                "hint": "No text could be extracted from this file.",
            })
        from document_ocr.models import OCRRun
        from document_ocr.tasks import process_ocr
        ocr_run = OCRRun.objects.create(
            project_id=file.project_id,
            service_id=file.service_id,
            client=getattr(request.user, "client", None),
            client_name=getattr(request.user, "email", "") or "user",
            status="Pending",
            ocr_option="Basic-ocr",
        )
        process_ocr.delay(ocr_run.id, file.id, "Basic-ocr")
        return Response({
            "ready": False,
            "queued": True,
            "ocr_run_id": str(ocr_run.id),
            "filename": file.filename,
            "file_id": file.id,
            "project_id": file.project_id,
            "service_id": file.service_id,
        }, status=202)

    @action(detail=False, methods=['post'], url_path='db-inspect')
    def db_inspect(self, request):
        """
        Lightweight peek into a table of one of the caller's saved connections.
        Body: { "connection_id": <id>, "table": "users" }. Sensitive columns are never returned.
        """
        if not safety.db_features_enabled():
            return Response({"error": "Database questions are turned off on this platform."}, status=403)
        blocked = _assistant_blocked(request.user)
        if blocked:
            return blocked
        if request.data.get("connection_string"):
            return Response({"error": "Raw connection strings are not accepted. Select a saved connection."}, status=400)
        table_name = request.data.get("table")
        connection_id = request.data.get("connection_id")
        if not table_name or not connection_id:
            return Response({"error": "connection_id and table are required"}, status=400)
        try:
            db_conn, url = _owned_connection_url(request, connection_id)
            preview = preview_table(url, table_name)
            preview["connection"] = db_conn.name
            preview["database"] = safety.describe(url)
            return Response(preview)
        except safety.UnsafeTarget as e:
            return Response({"error": str(e)}, status=400)
        except Exception as e:
            logger.info("DocuChat db-inspect failed: %s", type(e).__name__)
            return Response({"error": "The database could not be reached with those details."}, status=400)

