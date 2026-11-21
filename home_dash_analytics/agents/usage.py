"""
Every AI call, from every feature, is recorded once against the acting person and organisation.

`actor` is a context variable: web requests set it from the signed-in user (middleware below),
Celery tasks from their `user_id` argument (signal below). Model-calling code reports through
`record()`; calls with no identifiable organisation are logged as unattributed agent events so the
cost agent can show them instead of silently dropping them.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar

logger = logging.getLogger(__name__)

actor: ContextVar = ContextVar('ai_actor', default=None)
_recording: ContextVar = ContextVar('ai_usage_recorded_by_caller', default=False)


def _count(value):
    """A token count as int; anything a provider (or SDK version) reports that isn't a number counts as 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(int(value), 0)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return 0


def usage_numbers(response):
    """(prompt, completion, total) tokens from an OpenAI-compatible or Ollama response, 0 when unknown."""
    if isinstance(response, dict):  # Ollama /api/chat
        prompt, completion = _count(response.get('prompt_eval_count')), _count(response.get('eval_count'))
        return prompt, completion, prompt + completion
    usage = getattr(response, 'usage', None)
    if usage is not None:
        prompt = _count(getattr(usage, 'prompt_tokens', 0))
        completion = _count(getattr(usage, 'completion_tokens', 0))
        return prompt, completion, _count(getattr(usage, 'total_tokens', 0)) or prompt + completion
    return 0, 0, 0


def record(*, provider: str, model: str, task: str, prompt_tokens=0, completion_tokens=0, total_tokens=None,
           duration_ms=None, user=None, extra=None):
    """Write one usage row to the cost ledger (cost_centre EventLog + TokenUsage)."""
    user = user or actor.get()
    total = int(total_tokens if total_tokens is not None else (prompt_tokens or 0) + (completion_tokens or 0))
    from .gateway import current_key
    meta = {'provider': provider, 'model': model, 'task': task, 'usage_reported': bool(total), 'key': current_key(),
            'prompt_tokens': int(prompt_tokens or 0), 'completion_tokens': int(completion_tokens or 0),
            **({'duration_ms': int(duration_ms)} if duration_ms is not None else {}), **(extra or {})}
    tenant_id = getattr(user, 'client_id', None) if user is not None else None
    if user is None or tenant_id is None:
        from .events import emit
        emit('cost', 'usage.unattributed', severity='warning', tokens=total, **meta)
        return None
    try:
        from django.db import transaction
        from cost_centre.models import EventLog, TokenUsage, EventType, ServiceType
        with transaction.atomic():
            event = EventLog.objects.create(user=user, tenant_id=tenant_id, event_type=EventType.OTHER,
                                            service_type=ServiceType.PAYABLE, tokens_used=total, metadata=meta)
            TokenUsage.objects.create(user=user, tenant_id=tenant_id, event_log=event, tokens_used=total)
        from .gateway import ledger_changed
        ledger_changed(tenant_id)
        return event
    except Exception:
        logger.warning('Could not record AI usage for %s/%s', provider, model, exc_info=True)
        return None


def record_response(response, *, provider: str, model: str, task: str, started=None):
    """Record a completed call unless the caller records it itself (see `recorded_by_caller`)."""
    if _recording.get():
        return
    prompt, completion, total = usage_numbers(response)
    record(provider=provider, model=model, task=task, prompt_tokens=prompt, completion_tokens=completion,
           total_tokens=total, duration_ms=(time.monotonic() - started) * 1000 if started else None)


@contextmanager
def recorded_by_caller():
    """For code that records usage itself with richer metadata (the document intelligence agent)."""
    token = _recording.set(True)
    try:
        yield
    finally:
        _recording.reset(token)


@contextmanager
def acting_as(user):
    token = actor.set(user)
    try:
        yield
    finally:
        actor.reset(token)


class AIActorMiddleware:
    """Make the signed-in user the actor for AI calls made while handling the request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        token = actor.set(user if getattr(user, 'is_authenticated', False) else None)
        try:
            return self.get_response(request)
        finally:
            actor.reset(token)


def _task_actor(sender=None, kwargs=None, **_):
    """Celery: tasks that receive `user_id` act as that user for AI usage."""
    user_id = (kwargs or {}).get('user_id')
    if not user_id:
        actor.set(None)
        return
    try:
        from django.contrib.auth import get_user_model
        actor.set(get_user_model().objects.filter(pk=user_id).first())
    except Exception:
        actor.set(None)


def connect_celery_signals():
    from celery.signals import task_prerun
    task_prerun.connect(_task_actor, weak=False, dispatch_uid='ai-usage-actor')


# ── DSPy (private equity, IP litigation, regulatory compliance, ...) ─────────────────────────
_DSPY_PROVIDERS = {"litellm_proxy": "litellm", "ollama_chat": "ollama", "hosted_vllm": "vllm"}


def _dspy_model(lm_model: str):
    """(provider, model) for the ledger from a DSPy model string such as 'litellm_proxy/gpt-4o'."""
    name = str(lm_model or "")
    prefix, _, rest = name.partition("/")
    if not rest:
        return "litellm", name
    provider = _DSPY_PROVIDERS.get(prefix, prefix)
    # Through the proxy the model is the proxy's name; directly it keeps its provider prefix for pricing.
    return provider, rest if prefix in ("litellm_proxy", "ollama_chat", "hosted_vllm") else name


def instrument_dspy():
    """Every DSPy model call goes through the AI gateway (limits) and the cost ledger (tokens).

    DSPy 3.x runs each call as prepare -> execute -> finalize (dspy.clients.execution), for
    synchronous and asynchronous calls alike. The gateway check runs in prepare, before anything
    is sent; usage is recorded in finalize from the provider's reported input and output tokens.
    """
    try:
        import dspy.clients.execution as execution
    except Exception:  # DSPy not installed or a different major version: nothing to wrap
        return False
    if getattr(execution, '_aidoc_metered', False):
        return True
    original_prepare, original_finalize = execution.prepare, execution.finalize

    def prepare(lm, *args, **kwargs):
        from .gateway import enforce
        enforce()
        return original_prepare(lm, *args, **kwargs)

    def finalize(lm, call, result):
        try:
            if not getattr(result, 'cache_hit', False):
                usage = getattr(result, 'usage', None) or {}
                provider, model = _dspy_model(getattr(lm, 'model', ''))
                record(provider=provider, model=model, task='vertical-ai',
                       prompt_tokens=_count(usage.get('prompt_tokens')),
                       completion_tokens=_count(usage.get('completion_tokens')),
                       total_tokens=_count(usage.get('total_tokens')) or None)
        except Exception:
            logger.warning('Could not record DSPy usage', exc_info=True)
        return original_finalize(lm, call, result)

    execution.prepare, execution.finalize = prepare, finalize
    execution._aidoc_metered = True
    return True
