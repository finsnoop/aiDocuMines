"""Model discovery for the Assistant: lists the models a provider currently serves."""
import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from core.llm_providers import PROVIDERS, ProviderError, default_provider, list_models

logger = logging.getLogger(__name__)


class ModelListThrottle(UserRateThrottle):
    rate = "60/min"


class ProviderModelsView(APIView):
    """
    POST /api/v1/grid-documents-interrogation/proxy/models/
    Body: {"provider": "litellm|ollama|vllm|openai", "endpoint": optional, "api_key": optional}

    Returns the provider's model list in its native shape ({"data": [...]} or, for
    Ollama, {"models": [...]}). Keys and default endpoints come from server settings;
    a user-supplied endpoint is only honoured for allow-listed hosts.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ModelListThrottle]

    def post(self, request):
        provider = str(request.data.get("provider") or default_provider()).lower()
        if provider not in PROVIDERS:
            return Response(
                {"error": f"Unsupported provider '{provider}'. Use one of: {', '.join(PROVIDERS)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            data = list_models(provider, request.data.get("endpoint"), request.data.get("api_key"))
        except ProviderError as exc:
            logger.warning("Model list failed for provider=%s user=%s: %s", provider, request.user.pk, exc)
            return Response({"error": str(exc)}, status=exc.status)
        return Response(data)

    def get(self, request):
        """Convenience: list the default provider's models."""
        try:
            return Response(list_models(default_provider()))
        except ProviderError as exc:
            return Response({"error": str(exc)}, status=exc.status)
