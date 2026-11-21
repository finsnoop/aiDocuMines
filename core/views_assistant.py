"""
POST /api/v1/core/assistant/ask/
{
  "context":  {"type": "file|folder|matter|project|documents|data", "id": ..., "project_id": ..., "service_id": ...},
  "profile":  "contracts.contract",                              the Ask Assistant profile (core/ai_harness.PROFILES)
  "surface":  "documents|matters|contracts|...",                  the page, when no profile is given
  "question": "What is the notice period?",
  "history":  [{"role": "user|assistant", "content": "..."}],   optional; the organisation's history limit applies
  "web":      false,                                            include public web results
  "provider": "litellm|ollama|vllm|openai", "model": "..."      optional; server defaults otherwise
}
-> {"answer", "outcome", "sources": [{label, file_id, filename, excerpt}], "web_sources": [{label, title, url}],
    "web": "used|off|disabled|not_configured|not_permitted", "context": {type, label, files_in_scope, files_read}}

GET /api/v1/core/assistant/status/  -> what the caller may do (assistant, web, surfaces) for the UI.

Every answer goes through the AI harness (core/ai_harness.py): entitlement, input screening, evidence gate,
object-bound instruction, citation verification with a bounded repair loop, redaction and audit.

The Documents page uses {"type":"documents", "file_ids":[...]} for an explicit
selection (at most 200), or {"type":"documents", "filters":{...}} for its full
filtered library. Both are resolved on the server against the caller's ACLs.
"""
import logging
import os

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from core import ai_harness as harness
from core.access import user_has_perm
from core.assistant import assistant_model, build_sources, resolve_context
from core.llm_providers import available_chat_model, is_missing_model_error
from core.web_search import search as web_search
from core.web_search import web_status

logger = logging.getLogger(__name__)
MAX_QUESTION = harness.MAX_QUESTION


class AssistantThrottle(UserRateThrottle):
    rate = os.getenv("ASSISTANT_RATE", "30/min")


class ModelUnavailable(Exception):
    pass


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
        allowed = _assistant_allowed(request.user)
        surfaces = harness.settings_for(request.user)["surfaces"]
        return Response({"assistant": allowed, "web": _web_allowed(request.user),
                         "surfaces": {k: allowed and on for k, on in surfaces.items()},
                         "profiles": {k: p.surface for k, p in harness.PROFILES.items()}})


class AssistantAskView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [AssistantThrottle]

    def post(self, request):
        user = request.user
        spec = request.data.get("context") or {}
        if not isinstance(spec, dict):
            return Response({"error": "Provide a context object."}, status=status.HTTP_400_BAD_REQUEST)
        # The reusable Ask Assistant profile for this feature/object (core/ai_harness.py PROFILES).
        profile = harness.profile_for(str(request.data.get("profile") or "")[:60] or None,
                                      str(request.data.get("surface") or "")[:40] or None, str(spec.get("type") or ""))
        surface = profile.surface or str(request.data.get("surface") or "")[:40]
        # Layer 1: entitlement
        if not _assistant_allowed(user):
            return Response({"error": "The assistant is not available for your role or organisation."},
                            status=status.HTTP_403_FORBIDDEN)
        if not harness.surface_allowed(user, surface):
            return Response({"error": "Your administrator has turned off Ask Assistant on this page."},
                            status=status.HTTP_403_FORBIDDEN)
        settings = harness.settings_for(user, profile)
        # Layer 2: input screening
        question = str(request.data.get("question") or "").strip()
        if not question:
            return Response({"error": "Ask a question."}, status=status.HTTP_400_BAD_REQUEST)
        question = question[:MAX_QUESTION]
        flags = []
        if harness.screen_question(question):
            flags.append("injection")
            if settings["guardrails"]["prompt_injection"] == "block":
                ctx = resolve_context(user, spec)       # still 404 for objects the caller cannot open
                answer = ("That question asks me to set aside my rules, so I won't answer it. "
                          f'Ask about the content of "{ctx.label}" instead.')
                _log(user, ctx, spec, surface, question, 0, "off", "blocked_injection", flags, {"profile": profile.key})
                return Response(_body(answer, "blocked_injection", ctx, [], [], "off", None, None))

        # Layer 3: evidence (access-checked) and the evidence gate
        ctx = resolve_context(user, spec)               # raises 404 when not readable
        sources = build_sources(question, ctx, user)
        if not sources and not ctx.data:
            if ctx.kind == "documents":
                answer = ("No accessible documents match these filters. Change the filters or select documents to ask about."
                          if not ctx.files else
                          "No readable document text was found in this selection. Open the documents or run text extraction before asking about their contents.")
            else:
                answer = harness.no_evidence(ctx, settings)
            _log(user, ctx, spec, surface, question, 0, "off", "no_evidence", flags, {"profile": profile.key})
            return Response(_body(answer, "no_evidence", ctx, [], [], "off", None, None))

        web_state, web_results = "off", []
        if request.data.get("web"):
            web_state = _web_allowed(user)
            if web_state == "ok":
                web_results = web_search(question)     # only the question leaves the platform
                web_state = "used" if web_results else "no_results"

        # Layers 4-5: the loop (instruct, call, verify, repair)
        from grid_documents_interrogation.safety import clean_llm_config
        from grid_documents_interrogation.utils import dispatch_to_llm

        chosen = {"provider": None, "model": None}
        chosen["provider"], chosen["model"] = assistant_model(user, request.data.get("provider"), request.data.get("model"))

        def call(message, evidence, turns, system):
            config = clean_llm_config({"provider": chosen["provider"], "model": chosen["model"], "temperature": 0.1}, user)
            try:
                return dispatch_to_llm(message, evidence, config, turns, system_prompt=system)
            except Exception as exc:
                fallback = available_chat_model(chosen["provider"]) if is_missing_model_error(exc) else None
                if not fallback or fallback == chosen["model"]:
                    logger.warning("Assistant model call failed for user %s: %s", user.pk, exc)
                    raise ModelUnavailable() from exc
                logger.warning("Model %r unavailable (%s); using %r. Update ASSISTANT_MODEL/OPENAI_MODEL.",
                               chosen["model"], exc, fallback)
                chosen["model"] = fallback
                config = clean_llm_config({"provider": chosen["provider"], "model": fallback, "temperature": 0.1}, user)
                try:
                    return dispatch_to_llm(message, evidence, config, turns, system_prompt=system)
                except Exception as exc2:
                    logger.warning("Assistant model call failed for user %s: %s", user.pk, exc2)
                    raise ModelUnavailable() from exc2

        try:
            result = harness.run(ctx, question, sources, web_results, request.data.get("history"), settings, call)
        except ModelUnavailable:
            return Response({"error": "The AI model is not reachable right now. Please try again shortly."},
                            status=status.HTTP_502_BAD_GATEWAY)
        # Layer 6: audit
        _log(user, ctx, spec, surface, question, len(sources), web_state, result["outcome"], flags, result["trace"])
        return Response(_body(result["answer"], result["outcome"], ctx, sources, web_results, web_state,
                              chosen["provider"], chosen["model"]))


def _body(answer, outcome, ctx, sources, web_results, web_state, provider, model):
    return {
        "answer": answer,
        "outcome": outcome,
        "sources": sources,
        "web_sources": [{"label": f"W{i + 1}", "title": w["title"], "url": w["url"]} for i, w in enumerate(web_results)],
        "web": web_state,
        "context": _context_response(ctx),
        "model": {"provider": provider, "model": model},
    }


def _context_response(ctx):
    result = {"type": ctx.kind, "label": ctx.label, "files_in_scope": ctx.total_in_scope,
              "files_read": len(ctx.files)}
    if ctx.kind == "documents":
        result.update({"scope_mode": ctx.scope_mode, "count_is_capped": ctx.count_is_capped,
                       "coverage": "partial" if ctx.coverage_partial or ctx.count_is_capped or len(ctx.files) < ctx.total_in_scope else "complete"})
        if ctx.scope_mode == "filters":
            result["filters"] = ctx.filters
    return result


def _log(user, ctx, spec, surface, question, n_sources, web_state, outcome, flags, trace):
    try:
        from custom_authentication.models import UserActivityLog

        UserActivityLog.objects.create(user=user, event="CHAT_QUERY", metadata={
            "query_preview": question[:120], "context_type": ctx.kind, "context_label": ctx.label,
            "context_id": str(spec.get("id") or spec.get("project_id") or ""), "sources": n_sources, "web": web_state,
            "surface": surface, "profile": trace.get("profile") if trace else None, "outcome": outcome, "flags": flags, "trace": trace,
        })
    except Exception:
        logger.exception("Could not record assistant activity")
