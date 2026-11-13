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
            data = list_models(provider, *self._endpoint_and_key(request, provider))
            data = _only_allowed(request.user, provider, data)
        except ProviderError as exc:
            logger.warning("Model list failed for provider=%s user=%s: %s", provider, request.user.pk, exc)
            return Response({"error": str(exc)}, status=exc.status)
        return Response(data)

    @staticmethod
    def _endpoint_and_key(request, provider):
        """Organisation rules: personal keys/endpoints only where allowed; the organisation's key otherwise."""
        from custom_authentication.policies import ai_access, organisation_ai_key
        access = ai_access(request.user)
        endpoint, key = request.data.get("endpoint"), request.data.get("api_key")
        if not access["allow_personal"]:
            endpoint, key = None, None
        if not key and access["has_org_key"] and (not access["provider"] or access["provider"] == provider):
            key = organisation_ai_key(access["org_id"])
        return endpoint, key

    def get(self, request):
        """Convenience: list the default provider's models."""
        try:
            return Response(list_models(default_provider()))
        except ProviderError as exc:
            return Response({"error": str(exc)}, status=exc.status)


def _only_allowed(user, provider, data):
    """Non-admins only see the models their organisation allows (admins see all, to choose from)."""
    from core.access import is_tenant_admin
    from custom_authentication.policies import ai_access
    allowed = ai_access(user).get("allowed") or []
    if not allowed or is_tenant_admin(user):
        return data
    names = {m["model"] for m in allowed if (m["provider"] or provider) == provider}
    if "models" in data:
        return {"models": [m for m in data["models"] if (m.get("name") or m.get("model")) in names]}
    return {**data, "data": [m for m in data.get("data", []) if m.get("id") in names]}


class AvailableModelsView(APIView):
    """
    GET /api/v1/grid-documents-interrogation/models/available/
    The models this person may choose in the Assistant: the organisation's allow-list
    when its administrators set one, otherwise the chat models of the provider in use.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ModelListThrottle]

    def get(self, request):
        from core.llm_providers import NOT_CHAT
        from custom_authentication.policies import ai_access, organisation_ai_key
        access = ai_access(request.user)
        if access["allowed"]:
            provider = access["provider"] or default_provider()
            models = [{"provider": m["provider"] or provider, "model": m["model"]} for m in access["allowed"]]
            default = access["default_model"] if any(m["model"] == access["default_model"] for m in models)                 else models[0]["model"]
            return Response({"restricted": True, "models": models, "default": default})
        provider = access["provider"] or default_provider()
        key = organisation_ai_key(access["org_id"]) if access["has_org_key"] else None
        try:
            data = list_models(provider, None, key)
        except ProviderError as exc:
            return Response({"error": str(exc)}, status=exc.status)
        ids = [m.get("name") or m.get("model") for m in data.get("models", [])] if provider == "ollama"             else [m.get("id") for m in data.get("data", [])]
        ids = sorted(i for i in ids if i and "*" not in i and not any(w in i.lower() for w in NOT_CHAT))
        from core.llm_providers import pick_chat_model
        return Response({"restricted": False, "models": [{"provider": provider, "model": i} for i in ids],
                         "default": access["default_model"] or pick_chat_model(ids) or ""})
