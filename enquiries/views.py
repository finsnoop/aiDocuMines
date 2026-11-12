import logging
import os

from django.core.mail import send_mail
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .models import Enquiry

logger = logging.getLogger(__name__)


class EnquiryThrottle(AnonRateThrottle):
    rate = "5/hour"


class EnquirySerializer(serializers.ModelSerializer):
    # Honeypot: real visitors never see or fill this field.
    website = serializers.CharField(required=False, allow_blank=True, write_only=True)

    class Meta:
        model = Enquiry
        fields = ["name", "email", "company", "role", "topic", "practice_area", "message",
                  "consent", "source_page", "website"]

    def validate_consent(self, value):
        if not value:
            raise serializers.ValidationError("Please agree to be contacted so we can reply.")
        return value


class EnquiryCreateView(APIView):
    """Public endpoint for demo requests and contact messages from the website."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [EnquiryThrottle]

    def post(self, request):
        serializer = EnquirySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        if data.pop("website", ""):
            # Bot filled the honeypot: pretend success, store nothing.
            return Response({"ok": True}, status=status.HTTP_201_CREATED)

        enquiry = Enquiry.objects.create(
            **data,
            ip_address=(request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
                        or request.META.get("REMOTE_ADDR")) or None,
        )
        self._notify(enquiry)
        return Response({"ok": True, "id": enquiry.id}, status=status.HTTP_201_CREATED)

    @staticmethod
    def _notify(enquiry):
        to = os.getenv("SALES_NOTIFICATION_EMAIL", "")
        if not to:
            return
        try:
            send_mail(
                subject=f"[aiDocuMines] {enquiry.get_topic_display()}: {enquiry.name}, {enquiry.company or 'no company'}",
                message=(
                    f"Name: {enquiry.name}\nEmail: {enquiry.email}\nCompany: {enquiry.company}\n"
                    f"Role: {enquiry.role}\nTopic: {enquiry.get_topic_display()}\n"
                    f"Practice area: {enquiry.practice_area}\n\n{enquiry.message}\n"
                ),
                from_email=None,
                recipient_list=[a.strip() for a in to.split(",") if a.strip()],
                fail_silently=False,
            )
        except Exception:
            # The enquiry is saved; a mail outage must not lose it or fail the request.
            logger.exception("Could not send enquiry notification for enquiry %s", enquiry.id)
