"""
Server-side memory for Ask Assistant: one conversation per person per object, resumable across sessions.

- The conversation is keyed by the object's context (and profile), owned by one person, and returned only while
  that person can still open the object (the caller re-resolves the context first).
- The model sees the most recent turns (the organisation's `history_turns`); older turns are compacted into an
  extractive summary (earlier questions and the opening of each answer), so long conversations do not degrade.
- Conversations expire after the organisation's `memory_days` (0 = nothing is kept; the panel's own history is used)
  and are erased with the person's data (custom_authentication.privacy.erase).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta

from django.utils import timezone

SUMMARY_CHARS = 2000


def context_key(spec: dict, profile: str = "") -> str:
    clean = {k: spec.get(k) for k in sorted(spec) if k not in ("label", "data")}
    if isinstance(spec.get("data"), (dict, list)):
        clean["data"] = hashlib.sha256(json.dumps(spec["data"], sort_keys=True, default=str).encode()).hexdigest()
    return hashlib.sha256(json.dumps([clean, profile], sort_keys=True, default=str).encode()).hexdigest()


def _expired_before(settings: dict):
    days = int(settings["guardrails"].get("memory_days", 0) or 0)
    return (timezone.now() - timedelta(days=days)) if days else None


def find(user, spec: dict, profile: str, settings: dict):
    """The person's live conversation about this object, or None (memory off, none yet, or expired)."""
    from core.models import AssistantConversation
    cutoff = _expired_before(settings)
    if cutoff is None:
        return None
    key = context_key(spec, profile)
    conv = AssistantConversation.objects.filter(user=user, context_key=key).first()
    if conv is not None and conv.updated_at < cutoff:
        conv.delete()
        return None
    return conv


def history(conv, turns: int) -> list:
    if conv is None or not turns:
        return []
    recent = list(conv.turns.filter(compacted=False).order_by("-created_at", "-id")[: turns * 2])[::-1]
    return [{"role": t.role, "content": t.content[:2000]} for t in recent]


def transcript(conv) -> list:
    if conv is None:
        return []
    return [{"role": t.role, "content": t.content, "outcome": t.outcome, "sources": t.sources,
             "at": t.created_at.isoformat()} for t in conv.turns.all()]


def remember(conv, user, spec: dict, profile: str, label: str, settings: dict, question: str, answer: str,
             outcome: str, sources: list):
    """Add one exchange (creating the conversation if needed) and compact older turns. Returns the conversation."""
    from core.models import AssistantConversation, AssistantTurn
    if _expired_before(settings) is None:
        return None
    if conv is None:
        conv = AssistantConversation.objects.create(
            client_id=getattr(user, "client_id", None), user=user, profile=profile[:60],
            context_key=context_key(spec, profile), context={k: v for k, v in spec.items() if k != "data"},
            label=(label or "")[:255])
    AssistantTurn.objects.create(conversation=conv, role="user", content=question[:8000])
    AssistantTurn.objects.create(conversation=conv, role="assistant", content=(answer or "")[:20000],
                                 outcome=outcome[:30], sources=[{k: s.get(k) for k in ("label", "file_id", "filename")}
                                                                for s in (sources or [])][:40])
    compact(conv, settings["guardrails"]["history_turns"])
    conv.save(update_fields=["updated_at", "summary"])
    return conv


def _first_sentence(text: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    cut = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return cut[:limit]


def compact(conv, keep_turns: int) -> None:
    """Fold everything but the newest `keep_turns` exchanges into the summary (context compaction)."""
    live = list(conv.turns.filter(compacted=False).order_by("created_at", "id"))
    excess = len(live) - max(keep_turns, 0) * 2
    if excess <= 0:
        return
    old = live[:excess]
    lines = []
    for t in old:
        lines.append(("Q: " + _first_sentence(t.content, 200)) if t.role == "user" else ("A: " + _first_sentence(t.content)))
    summary = (conv.summary + "\n" if conv.summary else "") + "\n".join(lines)
    conv.summary = summary[-SUMMARY_CHARS:]
    conv.turns.filter(pk__in=[t.pk for t in old]).update(compacted=True)


def purge_expired(now=None) -> int:
    """Delete conversations past each organisation's memory period (daily, with privacy retention)."""
    from core import ai_harness
    from core.models import AssistantConversation
    from custom_authentication.models import OrganisationPolicy
    now = now or timezone.now()
    removed = 0
    default_days = ai_harness.GUARDRAIL_DEFAULTS["memory_days"]
    configured = {p.client_id: ai_harness.guardrails(p)["memory_days"] for p in OrganisationPolicy.objects.all()}
    for client_id in AssistantConversation.objects.values_list("client_id", flat=True).distinct():
        days = configured.get(client_id, default_days)
        qs = AssistantConversation.objects.filter(client_id=client_id)
        if days:
            qs = qs.filter(updated_at__lt=now - timedelta(days=days))
        ids = list(qs.values_list("pk", flat=True))
        removed += len(ids)
        AssistantConversation.objects.filter(pk__in=ids).delete()
    return removed
