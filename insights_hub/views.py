from __future__ import annotations
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.core.cache import cache
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from oauth2_provider.models import Application
from oauth2_provider.contrib.rest_framework import OAuth2Authentication, TokenHasReadWriteScope
from custom_authentication.permissions import IsClientOrAdminOrSuperUser

from .serializers import HomeInsightsSerializer
from .services import compute_insights
from .tasks import compute_insights_task

# ─────────────────────────────────────────────────────────────────────────────
# Header helper (same idea as your core.views)
def get_user_from_client_id(client_id: str):
    try:
        app = Application.objects.get(client_id=client_id)
        return app.user
    except Application.DoesNotExist:
        return None

# Swagger params (mirror your style)
client_id_param = openapi.Parameter(
    "X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=True,
    description="Client ID provided at signup"
)
from_param = openapi.Parameter(
    "from", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False,
    description="ISO date/datetime (inclusive) e.g., 2025-07-01 or 2025-07-01T00:00:00Z"
)
to_param = openapi.Parameter(
    "to", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False,
    description="ISO date/datetime (inclusive) e.g., 2025-08-17 or 2025-08-17T23:59:59Z"
)
project_param = openapi.Parameter(
    "project", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False,
    description="Filter by project_id"
)
service_param = openapi.Parameter(
    "service", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False,
    description="Filter by service_id"
)
async_param = openapi.Parameter(
    "async", openapi.IN_QUERY, type=openapi.TYPE_STRING, required=False,
    description="If '1' or 'true', compute asynchronously via Celery"
)

CACHE_TTL = 60  # seconds

# ─────────────────────────────────────────────────────────────────────────────
@method_decorator(csrf_exempt, name="dispatch")
class InsightsView(APIView):
    """
    Aggregated insights across apps (core, anonymizer, …) for the current client.
    Auth: OAuth2 Bearer + X-Client-ID header (same as other apps).
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Return aggregated dashboard insights for the current OAuth2 client.",
        tags=["Insights"],
        manual_parameters=[client_id_param, from_param, to_param, project_param, service_param, async_param],
        responses={200: "Success", 202: "Accepted (async)", 401: "Unauthorized", 400: "Bad Request"},
    )
    def get(self, request):
        # ── Auth headers (mirror your other views) ───────────────────────────
        client_id = request.headers.get("X-Client-ID")
        access_token = request.headers.get("Authorization", "").split("Bearer ")[-1]

        if not access_token:
            return Response({"error": "Authorization token missing"}, status=status.HTTP_401_UNAUTHORIZED)

        user = get_user_from_client_id(client_id)
        if not user:
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        # ── Query params / async switch ─────────────────────────────────────
        params = {
            "from": request.query_params.get("from"),
            "to": request.query_params.get("to"),
            "project_id": request.query_params.get("project"),
            "service_id": request.query_params.get("service"),
        }
        use_async = request.query_params.get("async") in ("1", "true", "True")

        if use_async:
            task = compute_insights_task.delay(user.id, params)
            return Response({"task_id": task.id}, status=status.HTTP_202_ACCEPTED)

        # ── Cache then compute (fast path) ──────────────────────────────────
        key = f"insights:{user.id}:{params}"
        data = cache.get(key)
        if not data:
            data = compute_insights({"user": user, **params})
            cache.set(key, data, CACHE_TTL)

        # Shape/validate output
        serializer = HomeInsightsSerializer(instance=data)
        return Response(serializer.data, status=status.HTTP_200_OK)

# ─────────────────────────────────────────────────────────────────────────────
@method_decorator(csrf_exempt, name="dispatch")
class InsightsTaskStatusView(APIView):
    """
    Poll Celery async computation (simple cache-based handoff).
    """
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope, IsClientOrAdminOrSuperUser]

    @swagger_auto_schema(
        operation_description="Check the status/result for an async insights compute task.",
        tags=["Insights"],
        manual_parameters=[
            client_id_param,
            openapi.Parameter("task_id", openapi.IN_PATH, type=openapi.TYPE_STRING, required=True,
                              description="Celery task id returned by async=1 call"),
        ],
        responses={200: "Success", 202: "Pending"},
    )
    def get(self, request, task_id: str):
        # Optional: enforce header presence the same way
        client_id = request.headers.get("X-Client-ID")
        access_token = request.headers.get("Authorization", "").split("Bearer ")[-1]
        if not access_token:
            return Response({"error": "Authorization token missing"}, status=status.HTTP_401_UNAUTHORIZED)
        if not get_user_from_client_id(client_id):
            return Response({"error": "Invalid client ID"}, status=status.HTTP_401_UNAUTHORIZED)

        data = cache.get(f"insights_task:{task_id}")
        if not data:
            return Response({"task_id": task_id, "state": "PENDING"}, status=status.HTTP_202_ACCEPTED)

        serializer = HomeInsightsSerializer(instance=data)
        return Response({"task_id": task_id, "state": "SUCCESS", "result": serializer.data}, status=status.HTTP_200_OK)

