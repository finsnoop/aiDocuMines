"""
AI building blocks for the practice-area pipelines (Private Equity, Labor &
Employment, Regulatory Compliance, IP Litigation).

Those pipelines call `semantic_search_task.delay(...).get()`, `ask_question_task...`
and `detect_pii_task...`. These names never existed, so every pipeline silently
fell back to placeholder results. They are provided here on the platform's real
services:

* semantic search -> document_search.exec_search (Milvus + permission filter)
* questions       -> the DocuChat text cache + configured model (dispatch_to_llm)
* PII detection   -> the shared Presidio analyzer used by the anonymiser

The work runs in-process inside the pipeline's own Celery task. `.delay()` returns
an already-finished result whose `.get()` hands it back, so a worker never blocks
waiting for another worker (which can deadlock a busy pool). Every call is limited
to documents the user may read.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

MAX_CHARS_PER_FILE = 40_000
MAX_CHARS_TOTAL = 120_000
MAX_FILES = 20


class _Done:
    """A finished result with the AsyncResult surface the pipelines use."""

    def __init__(self, value):
        self._value = value

    def get(self, timeout=None, **_):
        return self._value

    def ready(self):
        return True

    successful = ready


class _InlineTask:
    def __init__(self, fn):
        self._fn = fn
        self.__name__ = fn.__name__
        self.__doc__ = fn.__doc__

    def __call__(self, *args, **kwargs):
        return self._fn(*args, **kwargs)

    def delay(self, *args, **kwargs):
        return _Done(self._fn(*args, **kwargs))

    def apply_async(self, args=(), kwargs=None, **_):
        return _Done(self._fn(*args, **(kwargs or {})))


def _user(user_id):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.filter(pk=user_id).first()


def _readable_files(user, file_ids: Iterable[int]):
    from core.access import accessible_files
    ids = [int(i) for i in file_ids if i is not None][:MAX_FILES]
    if not user or not ids:
        return []
    return list(accessible_files(user).filter(id__in=ids))


def _text_of(file_obj) -> str:
    from grid_documents_interrogation.utils import _get_or_build_cached_text_for_file_id
    try:
        text = _get_or_build_cached_text_for_file_id(file_obj.id)
    except Exception:
        text = file_obj.content or ""
    return (text or "")[:MAX_CHARS_PER_FILE]


# ── Semantic search ─────────────────────────────────────────────────────────
def semantic_search(query: str = "", top_k: int = 10, user_id: Optional[int] = None,
                    file_id: Optional[int] = None, **_) -> dict:
    """{"results": [...]} of passages the user may read (Milvus semantic search)."""
    if not user_id or not (query or "").strip():
        return {"results": []}
    from document_search.tasks import exec_search
    try:
        results = exec_search(int(user_id), query[:2000], file_id, max(1, min(int(top_k or 10), 50)))
    except Exception as exc:
        logger.warning("Semantic search unavailable for practice-area pipeline: %s", exc)
        results = []
    return {"results": results or []}


# ── Questions over documents ────────────────────────────────────────────────
def ask_question(query: str = "", file_ids: Iterable[int] = (), user_id: Optional[int] = None, **_) -> dict:
    """{"answer", "confidence", "file_ids"}: the configured model answers from the user's readable documents."""
    user = _user(user_id)
    files = _readable_files(user, file_ids or [])
    if not files or not (query or "").strip():
        return {"answer": "", "confidence": 0.0, "file_ids": []}

    parts, used = [], 0
    for f in files:
        text = _text_of(f)
        if not text.strip():
            continue
        block = f"### {f.filename}\n{text}"
        if used + len(block) > MAX_CHARS_TOTAL:
            block = block[: max(0, MAX_CHARS_TOTAL - used)]
        parts.append(block)
        used += len(block)
        if used >= MAX_CHARS_TOTAL:
            break
    if not parts:
        return {"answer": "", "confidence": 0.0, "file_ids": [f.id for f in files]}

    from custom_authentication.policies import assistant_policy
    from grid_documents_interrogation.safety import clean_llm_config
    from grid_documents_interrogation.utils import dispatch_to_llm

    cfg = clean_llm_config({"model": assistant_policy(user).get("model") or ""})
    try:
        answer = dispatch_to_llm(query[:4000], "\n\n".join(parts), cfg, previous_messages=None)
    except Exception as exc:
        logger.warning("Practice-area question failed: %s", exc)
        return {"answer": "", "confidence": 0.0, "file_ids": [f.id for f in files]}
    answer = (answer or "").strip()
    return {"answer": answer, "confidence": 0.75 if answer else 0.0, "file_ids": [f.id for f in files]}


# ── PII detection ───────────────────────────────────────────────────────────
def detect_pii(file_id: Optional[int] = None, detection_threshold: float = 0.8,
               user_id: Optional[int] = None, **_) -> dict:
    """{"detections": [{"type", "text", "confidence", "location": {"start", "end"}}]} via Presidio."""
    from core.models import File
    f = File.objects.filter(pk=file_id).first() if file_id else None
    if f is None:
        return {"detections": []}
    if user_id is not None and not _readable_files(_user(user_id), [f.id]):
        return {"detections": []}
    text = _text_of(f)
    if not text.strip():
        return {"detections": []}
    try:
        from document_anonymizer.utils import get_shared_anonymization_service
        service = get_shared_anonymization_service()
        hits = service.analyzer.analyze(text=text, entities=service._get_presidio_pii_entities(), language="en")
    except Exception as exc:
        logger.warning("PII detection unavailable: %s", exc)
        return {"detections": []}
    threshold = float(detection_threshold or 0.0)
    return {"detections": [
        {"type": h.entity_type, "text": text[h.start:h.end], "confidence": round(float(h.score), 3),
         "location": {"start": h.start, "end": h.end}}
        for h in hits if h.score >= threshold
    ][:500]}


semantic_search_task = _InlineTask(semantic_search)
ask_question_task = _InlineTask(ask_question)
detect_pii_task = _InlineTask(detect_pii)
