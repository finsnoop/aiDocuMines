# platform_data_insights/views.py

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from oauth2_provider.contrib.rest_framework import TokenHasReadWriteScope
from custom_authentication.authentication import ClientBoundOAuth2Authentication as OAuth2Authentication

from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi

from .models import UserInsights
from .serializers import UserInsightsSerializer, InsightsResponseSerializer
from .tasks import generate_insights_for_user
from .utils import calculate_user_insights

# Swagger parameter
client_id_param = openapi.Parameter(
    "X-Client-ID", openapi.IN_HEADER, type=openapi.TYPE_STRING, required=False, description="(Optional) OAuth2 client_id; if provided it must match the client that issued the access token."
)


def _resolve_token_user(request):
    """
    Resolve the caller strictly from the OAuth2 access token.

    The X-Client-ID header is untrusted; if supplied it must match the client
    that issued the token, otherwise the request is rejected. This prevents a
    user with any valid token from reading another tenant's data by spoofing
    the header.
    """
    header_client_id = request.headers.get("X-Client-ID")
    token = getattr(request, "auth", None)
    token_app = getattr(token, "application", None)

    if header_client_id and token_app is not None:
        if getattr(token_app, "client_id", None) != header_client_id:
            return None, Response(
                {"error": "Client mismatch for this token"},
                status=status.HTTP_403_FORBIDDEN,
            )

    if getattr(request, "user", None) and request.user.is_authenticated:
        return request.user, None

    # Fallback: client-credentials token has no bound user; use the app owner.
    if token_app is not None and getattr(token_app, "user", None):
        return token_app.user, None

    return None, Response(
        {"error": "User-bound access token required"},
        status=status.HTTP_403_FORBIDDEN,
    )

class PlatformInsightsView(APIView):
    authentication_classes = [OAuth2Authentication]
    permission_classes = [TokenHasReadWriteScope]

    @swagger_auto_schema(
        operation_description="Fetch or compute platform insights for the current user.",
        tags=["Platform Data Insights"],
        manual_parameters=[
            client_id_param,
            openapi.Parameter("async", openapi.IN_QUERY, type=openapi.TYPE_BOOLEAN, description="Run asynchronously"),
        ],
        responses={
            200: openapi.Response(
                description="Insights returned successfully",
                schema=InsightsResponseSerializer()
            ),
            202: openapi.Response(
                description="Insights computation queued"
            ),
        },
    )
    def get(self, request):
        user, err = _resolve_token_user(request)
        if err is not None:
            return err

        async_flag = request.query_params.get("async", "false").lower() == "true"

        if async_flag:
            # 🔹 Enqueue a Celery task
            task = generate_insights_for_user.delay(user.id)
            return Response({
                "message": "Insights computation queued.",
                "task_id": task.id
            }, status=status.HTTP_202_ACCEPTED)

        # 🔹 Check for cached insights
        latest = UserInsights.objects.filter(user=user).order_by("-generated_at").first()

        if latest:
            payload = {
                "cached": True,
                "generated_at": latest.generated_at,
                "insights": latest.insights_data,
            }
            serializer = InsightsResponseSerializer(payload)
            return Response(serializer.data, status=status.HTTP_200_OK)

        # 🔹 Otherwise compute live
        insights = calculate_user_insights(user)

        # Save for caching
        insights_obj = UserInsights.objects.create(
            user=user,
            insights_data=insights,
            generated_async=False,
        )

        # Return using serializer
        payload = {
            "cached": False,
            "generated_at": insights_obj.generated_at,
            "insights": insights,
        }
        serializer = InsightsResponseSerializer(payload)
        return Response(serializer.data, status=status.HTTP_200_OK)

