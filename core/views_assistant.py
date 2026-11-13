"""
POST /api/v1/core/assistant/ask/
{
  "context":  {"type": "file|folder|matter|project", "id": ..., "project_id": ..., "service_id": ...},
  "question": "What is the notice period?",
  "history":  [{"role": "user|assistant", "content": "..."}],   optional, last 10 used
  "web":      false,                                            include public web results
  "provider": "litellm|ollama|vllm|openai", "model": "..."      optional; server defaults otherwise
}
-> {"answer", "sources": [{label, file_id, filename, excerpt}], "web_sources": [{label, title, url}],
    "web": "used|off|disabled|not_configured|not_permitted", "context": {type, label, files_in_scope, files_read}}

GET /api/v1/core/assistant/status/  -> what the caller may do (assistant, web) for the UI.
"""
import logging
import os

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from core.access import user_has_perm
from core.assistant import build_sources, compose_prompt, resolve_context
from core.llm_providers import available_chat_model, default_provider, is_missing_model_error
from core.web_search import search as web_search
from core.web_search import web_status

logger = logging.getLogger(__name__)
MAX_QUESTION = 4000


class AssistantThrottle(UserRateThrottle):
    rate = os.getenv("ASSISTANT_RATE", "30/min")


def _web_allowed(user):
    from custom_authentication.policies import assistant_policy

    if not user_has_perm(user, "assistant.web"):
        return "not_permitted"
    if not assistant_policy(user)["web"]:
        return "disabled"
    return web_status()


def _assistant_allowed(user):
    from custom_authentication.policies import assistant_policy

    return user_has_perm(user, "assistant.ask") and assistant_policy(user)["enabled"]


class AssistantStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"assistant": _assistant_allowed(request.user),
                         "web": _web_allowed(request.user)})


class AssistantAskView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [AssistantThrottle]

    def post(self, request):
        from grid_documents_interrogation.utils import dispatch_to_llm

        user = request.user
        if not _assistant_allowed(user):
            return Response({"error": "The assistant is not available for your role or organisation."},
                            status=status.HTTP_403_FORBIDDEN)
        question = str(request.data.get("question") or "").strip()
        if not question:
            return Response({"error": "Ask a question."}, status=status.HTTP_400_BAD_REQUEST)
        question = question[:MAX_QUESTION]

        ctx = resolve_context(user, request.data.get("context") or {})   # raises 404 when not readable
        sources = build_sources(question, ctx, user)

        web_state, web_results = "off", []
        if request.data.get("web"):
            web_state = _web_allowed(user)
            if web_state == "ok":
                web_results = web_search(question)     # only the question leaves the platform
                web_state = "used" if web_results else "no_results"

        query, context_block = compose_prompt(question, ctx, sources, web_results)
        history = [
            {"role": m.get("role"), "content": str(m.get("content", ""))[:4000]}
            for m in (request.data.get("history") or [])[-10:]
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")
        ]
        provider = str(request.data.get("provider") or default_provider()).lower()
        from grid_documents_interrogation.safety import clean_llm_config
        from custom_authentication.policies import assistant_policy

        model = (request.data.get("model") or assistant_policy(user)["model"] or os.getenv("ASSISTANT_MODEL")
                 or os.getenv("OPENAI_MODEL") or "gpt-4o-mini")
        try:
            answer = dispatch_to_llm(query, context_block, clean_llm_config({"provider": provider, "model": model}, user), history)
        except Exception as exc:
            fallback = available_chat_model(provider) if is_missing_model_error(exc) else None
            if fallback and fallback != model:
                logger.warning("Model %r unavailable (%s); using %r. Update ASSISTANT_MODEL/OPENAI_MODEL.", model, exc, fallback)
                model = fallback
                try:
                    answer = dispatch_to_llm(query, context_block, clean_llm_config({"provider": provider, "model": model}, user), history)
                except Exception as exc2:
                    logger.warning("Assistant model call failed for user %s: %s", user.pk, exc2)
                    return Response({"error": "The AI model is not reachable right now. Please try again shortly."},
                                    status=status.HTTP_502_BAD_GATEWAY)
            else:
                logger.warning("Assistant model call failed for user %s: %s", user.pk, exc)
                return Response({"error": "The AI model is not reachable right now. Please try again shortly."},
                                status=status.HTTP_502_BAD_GATEWAY)

        _log(user, ctx, request.data.get("context") or {}, question, len(sources), web_state)
        return Response({
            "answer": answer,
            "sources": sources,
            "web_sources": [{"label": f"W{i + 1}", "title": w["title"], "url": w["url"]} for i, w in enumerate(web_results)],
            "web": web_state,
            "context": {"type": ctx.kind, "label": ctx.label, "files_in_scope": ctx.total_in_scope,
                        "files_read": len(ctx.files)},
            "model": {"provider": provider, "model": model},
        })


def _log(user, ctx, spec, question, n_sources, web_state):
    try:
        from custom_authentication.models import UserActivityLog

        UserActivityLog.objects.create(user=user, event="CHAT_QUERY", metadata={
            "query_preview": question[:120], "context_type": ctx.kind, "context_label": ctx.label,
            "context_id": str(spec.get("id") or spec.get("project_id") or ""), "sources": n_sources, "web": web_state,
        })
    except Exception:
        logger.exception("Could not record assistant activity")
