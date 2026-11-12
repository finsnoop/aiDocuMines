from __future__ import annotations

from datetime import datetime
from django.utils import timezone
from django.db.models import Q
from django.db import models
from rest_framework.viewsets import ModelViewSet
from rest_framework.permissions import IsAuthenticated
from rest_framework.filters import SearchFilter, OrderingFilter
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status
from custom_authentication.permissions import IsClientOrAdminOrSuperUser

from .models import EmailTemplate, OutboxEmail
from .serializers import (
    EmailTemplateSerializer,
    OutboxEmailSerializer,
    OutboxEmailCreateSerializer,
    RenderPreviewSerializer,
)
from .tasks import send_outbox_email


class EmailTemplateViewSet(ModelViewSet):
    """
    CRUD for email templates.
    """
    queryset = EmailTemplate.objects.all().order_by("-created_at")
    serializer_class = EmailTemplateSerializer
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ["code", "name", "description"]
    ordering = ["-created_at"]


class OutboxEmailViewSet(ModelViewSet):
    """
    Queue of emails to be rendered/sent via Celery.
    """
    queryset = OutboxEmail.objects.all().order_by("-created_at")
    permission_classes = [IsAuthenticated, IsClientOrAdminOrSuperUser]
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ["subject", "email_type", "status", "message_id", "provider_id"]
    ordering = ["-created_at"]

    # ---------- helpers ----------

    def _parse_dt(self, s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            # Accept ISO 8601 or naive 'YYYY-MM-DD' dates
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    def get_queryset(self):
        qs = super().get_queryset().select_related("template", "user")

        # Optional filters
        params = self.request.query_params

        status_q = params.get("status")
        if status_q:
            qs = qs.filter(status=status_q)

        email_type = params.get("email_type")
        if email_type:
            qs = qs.filter(email_type=email_type)

        template_id = params.get("template")
        if template_id:
            qs = qs.filter(template_id=template_id)

        template_code = params.get("template_code")
        if template_code:
            qs = qs.filter(template__code=template_code)

        created_from = self._parse_dt(params.get("created_from"))
        if created_from:
            qs = qs.filter(created_at__gte=created_from)

        created_to = self._parse_dt(params.get("created_to"))
        if created_to:
            qs = qs.filter(created_at__lte=created_to)

        sched_before = self._parse_dt(params.get("scheduled_before"))
        if sched_before:
            qs = qs.filter(scheduled_at__lte=sched_before)

        sched_after = self._parse_dt(params.get("scheduled_after"))
        if sched_after:
            qs = qs.filter(scheduled_at__gte=sched_after)

        # Simple contains search across recipients if provided
        addr_icontains = params.get("to_icontains")
        if addr_icontains:
            # Works on Postgres JSON as text search
            qs = qs.filter(Q(to__icontains=addr_icontains) |
                           Q(cc__icontains=addr_icontains) |
                           Q(bcc__icontains=addr_icontains))
        return qs

    def get_serializer_class(self):
        # Use the write-serializer for create/update so template_code works
        if self.action in ("create", "update", "partial_update"):
            return OutboxEmailCreateSerializer
        return OutboxEmailSerializer

    # ---------- CRUD overrides ----------

    def create(self, request, *args, **kwargs):
        """
        Use the write-serializer to accept template_code/context,
        then re-serialize with the read serializer (returns id, status, etc.).
        """
        write_ser = OutboxEmailCreateSerializer(data=request.data, context={"request": request})
        write_ser.is_valid(raise_exception=True)
        instance = write_ser.save(user=request.user if request.user.is_authenticated else None)

        # Auto-queue sending if scheduled_at is now/past
        if instance.scheduled_at and instance.scheduled_at <= timezone.now():
            send_outbox_email.delay(instance.pk)

        read_ser = OutboxEmailSerializer(instance, context={"request": request})
        headers = self.get_success_headers(read_ser.data)
        return Response(read_ser.data, status=status.HTTP_201_CREATED, headers=headers)

    def update(self, request, *args, **kwargs):
        """
        Allow updating with template_code; return the full read serialization.
        """
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        write_ser = OutboxEmailCreateSerializer(instance, data=request.data, partial=partial, context={"request": request})
        write_ser.is_valid(raise_exception=True)
        instance = write_ser.save()
        read_ser = OutboxEmailSerializer(instance, context={"request": request})
        return Response(read_ser.data)

    # ---------- actions ----------

    @action(detail=True, methods=["post"], url_path="send-now")
    def send_now(self, request, pk=None):
        """
        Force send this queued email ASAP.
        """
        out = self.get_object()
        out.scheduled_at = timezone.now()
        out.save(update_fields=["scheduled_at"])
        send_outbox_email.delay(out.pk)
        return Response({"queued": True, "id": out.pk}, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=["post"], url_path="send-due")
    def send_due(self, request):
        """
        Enqueue all due (pending) emails scheduled at or before now.
        """
        now = timezone.now()
        due_qs = self.get_queryset().filter(
            status=OutboxEmail.STATUS_PENDING,
            scheduled_at__lte=now,
        ).order_by("priority", "scheduled_at", "id")

        count = 0
        for out in due_qs.values_list("id", flat=True):
            send_outbox_email.delay(out)
            count += 1
        return Response({"queued": count}, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=["post"], url_path="retry-failed")
    def retry_failed(self, request):
        """
        Reset FAILED emails (within max_attempts) back to pending and enqueue.
        Optional body: {"limit": 100}
        """
        limit = int(request.data.get("limit", 100))
        candidates = self.get_queryset().filter(
            status=OutboxEmail.STATUS_FAILED,
            attempt_count__lt=models.F("max_attempts"),
        ).order_by("-updated_at")[:limit]

        reset_ids = []
        now = timezone.now()
        for out in candidates:
            out.status = OutboxEmail.STATUS_PENDING
            out.scheduled_at = now
            out.save(update_fields=["status", "scheduled_at", "updated_at"])
            reset_ids.append(out.id)
            send_outbox_email.delay(out.id)

        return Response({"reset_and_queued": reset_ids}, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        """
        Mark a pending email as cancelled (won't be sent by workers).
        """
        out = self.get_object()
        if out.status != OutboxEmail.STATUS_PENDING:
            return Response(
                {"detail": f"Only pending emails can be cancelled (current status={out.status})."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        out.status = OutboxEmail.STATUS_CANCELLED
        out.save(update_fields=["status", "updated_at"])
        return Response({"cancelled": True, "id": out.pk}, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="render-preview")
    def render_preview(self, request):
        """
        Render a template+context without saving an OutboxEmail.
        Body:
        {
          "template_code": "support_ticket_opened",
          "context": { ... }
        }
        """
        ser = RenderPreviewSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        preview = ser.save()
        return Response(preview, status=status.HTTP_200_OK)

