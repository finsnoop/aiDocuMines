"""
Context assistant: answer a question about one file, folder, matter or project,
using only documents the caller is allowed to read.

Access is resolved with the same rules as every other endpoint (organisation,
role, ownership or sharing). Anything the caller cannot open is left out of
the context; a context the caller cannot reach at all is a 404.

Context engineering: documents are split into passages, ranked against the
question (semantic search when available, keyword relevance otherwise), and
the best passages fill a fixed budget, each labelled [S1], [S2]... so the answer
can cite them. Large folders and matters are bounded (files scanned, text per
file, total context), so a request never loads an unbounded amount of data.
"""
from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field

from django.http import Http404
from rest_framework.exceptions import ValidationError

from core.access import accessible_files, get_file_for_user, is_platform_superuser, is_tenant_admin, tenant_id
from core.models import File

logger = logging.getLogger(__name__)

MAX_FILES_SCANNED = 200          # documents read for one question
MAX_TEXT_PER_FILE = 200_000      # characters read per document
MAX_CONTEXT_CHARS = 14_000       # characters of passages sent to the model
CHUNK_SIZE = 1_200
CHUNK_OVERLAP = 150
MAX_DATA_CHARS = 20_000          # on-screen data (a metric, report, chart or table row) sent with a question
MAX_DATA_FILES = 20              # documents attached to a data object

STOPWORDS = set("""a an and are as at be by for from has have in is it its of on or that the this to was were will with
what which who whom whose when where why how do does did can could should would may might shall i we you they he she
them our your their about into over under than then there these those not no yes all any each more most other some such
only own same so too very just also please tell me give list show summarise summarize explain""".split())


@dataclass
class Context:
    kind: str
    label: str
    files: list = field(default_factory=list)   # File objects the caller may read (bounded)
    total_in_scope: int = 0
    data: str = ''                              # "data" contexts: the object as shown to the caller (JSON)
    count_is_capped: bool = False
    scope_mode: str = ''
    filters: dict = field(default_factory=dict)
    coverage_partial: bool = False


# ── scope resolution ─────────────────────────────────────────────────────────

def _folder_ids_below(root_id) -> list:
    from document_operations.document_query import folder_ids_below
    return folder_ids_below(root_id)


def resolve_context(user, spec: dict) -> Context:
    """spec: {"type": file|folder|matter|project, "id": ..., "project_id": ..., "service_id": ...}."""
    if not isinstance(spec, dict):
        raise ValidationError({"context": "Provide a context object."})
    kind = spec.get("type")
    readable = accessible_files(user).exclude(document_type="deliverable")

    if kind == "documents":
        from document_operations.document_query import (
            COUNT_CAP, accessible_document_query, document_filter_label, document_filters,
        )
        if set(spec) - {"type", "file_ids", "filters", "label"}:
            raise ValidationError({"context": "Unknown document context field."})
        filters = document_filters(spec.get("filters"))
        if "file_ids" in spec:
            ids = spec["file_ids"]
            if not isinstance(ids, list) or not ids or len(ids) > MAX_FILES_SCANNED:
                raise ValidationError({"file_ids": f"Select between 1 and {MAX_FILES_SCANNED} documents."})
            if any(isinstance(i, bool) or not isinstance(i, (int, str)) or len(str(i)) > 19 or not str(i).isascii()
                   or not str(i).isdigit() or int(i) <= 0 or int(i) > 9223372036854775807 for i in ids):
                raise ValidationError({"file_ids": "Provide positive integer document IDs."})
            ids = list(dict.fromkeys(int(i) for i in ids))
            files = list(accessible_document_query(user).filter(pk__in=ids))
            if len(files) != len(ids):
                raise Http404("One or more documents were not found.")
            by_id = {f.pk: f for f in files}
            files = [by_id[i] for i in ids]
            label = files[0].filename if len(files) == 1 else f"{len(files)} selected documents"
            return Context("documents", label, files, len(files), scope_mode="selection")
        ctx = Context("documents", document_filter_label(filters), scope_mode="filters", filters=filters)
        return _bounded(ctx, accessible_document_query(user, filters), preserve_order=True, count_cap=COUNT_CAP)

    if kind == "file":
        f = get_file_for_user(user, spec.get("id"), "view")  # 404 if not readable
        return Context("file", f.filename, [f], 1)

    if kind == "folder":
        from document_operations.models import FileFolderLink, Folder

        try:
            folder = Folder.objects.select_related("user").get(id=uuid.UUID(str(spec.get("id"))))
        except (ValueError, TypeError, Folder.DoesNotExist):
            raise Http404("Folder not found")
        file_ids = FileFolderLink.objects.filter(folder_id__in=_folder_ids_below(folder.id), is_trashed=False) \
            .values("file_id")
        qs = readable.filter(id__in=file_ids)
        owner_side = (folder.user_id == user.id or
                      (is_tenant_admin(user) and getattr(folder.user, "client_id", None) == tenant_id(user)))
        if not owner_side and not qs.exists():
            raise Http404("Folder not found")  # no visible content and not theirs: do not reveal it exists
        return _bounded(Context("folder", folder.name), qs)

    if kind == "matter":
        from core.views_matters import _get_matter, _matter_files

        matter = _get_matter(user, spec.get("id"))  # 404 unless on the team / org admin
        return _bounded(Context("matter", matter.name), _matter_files(matter).filter(id__in=readable.values("id")))

    if kind == "project":
        project = str(spec.get("project_id") or "").strip()
        if not project:
            raise Http404("Nothing selected")
        qs = readable.filter(project_id=project)
        label = project
        if spec.get("service_id"):
            qs = qs.filter(service_id=str(spec["service_id"]))
            label = f"{project} / {spec['service_id']}"
        if not qs.exists():
            raise Http404("Nothing to read here")
        return _bounded(Context("project", label), qs)

    if kind == "data":
        # Any object on screen (a metric, a report, a chart's figures, a table row): the caller sends what they
        # see. Only the documents among `file_ids` that the caller may read are added; others are ignored.
        import json
        label = str(spec.get("label") or "Data")[:200]
        try:
            data = json.dumps(spec.get("data"), ensure_ascii=False, default=str, indent=1)
        except (TypeError, ValueError):
            data = str(spec.get("data"))
        ctx = Context("data", label, data=data[:MAX_DATA_CHARS])
        ids = [int(i) for i in (spec.get("file_ids") or []) if str(i).isdigit()][:MAX_DATA_FILES]
        if ids:
            _bounded(ctx, readable.filter(id__in=ids))
        return ctx

    raise Http404("Choose a file, folder or matter")


def _bounded(ctx: Context, qs, *, preserve_order=False, count_cap=None) -> Context:
    total = qs.order_by()[:count_cap + 1].count() if count_cap is not None else qs.count()
    ctx.count_is_capped = count_cap is not None and total > count_cap
    ctx.total_in_scope = min(total, count_cap) if count_cap is not None else total
    ctx.files = list((qs if preserve_order else qs.order_by("-updated_at", "-id"))[:MAX_FILES_SCANNED])
    return ctx


# ── text and passages ────────────────────────────────────────────────────────

def file_text(f: File) -> str:
    """Stored extracted text, or extract once and keep it (no save signals)."""
    if (f.content or "").strip():
        return f.content[:MAX_TEXT_PER_FILE]
    try:
        from document_search.text_policy import shared_file_text
        text = shared_file_text(f, allow_ocr=True)
    except Exception as exc:
        logger.info("Text extraction failed for file %s: %s", f.id, exc)
        text = ""
    return text[:MAX_TEXT_PER_FILE]


def chunks_of(text: str):
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    step = CHUNK_SIZE - CHUNK_OVERLAP
    for start in range(0, max(len(text), 1), step):
        piece = text[start:start + CHUNK_SIZE].strip()
        if piece:
            yield piece


def _terms(text: str) -> list:
    return [w for w in re.findall(r"[a-z0-9]{2,}", (text or "").lower()) if w not in STOPWORDS]


def rank_passages(question: str, passages: list) -> list:
    """BM25-style keyword relevance. passages: [(file, text)] -> same list, best first."""
    q = set(_terms(question))
    if not q:
        return passages
    docs = [Counter(_terms(t)) for _, t in passages]
    n = len(docs) or 1
    avg = sum(sum(d.values()) for d in docs) / n or 1
    df = Counter(term for d in docs for term in q if term in d)
    scored = []
    for (f, t), d in zip(passages, docs):
        length = sum(d.values()) or 1
        score = 0.0
        for term in q:
            tf = d.get(term, 0)
            if tf:
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / avg))
        scored.append((score, f, t))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [(f, t) for _, f, t in scored]


SEMANTIC_DEADLINE_S = float(os.getenv("SEMANTIC_SEARCH_DEADLINE_S", "4"))
SEMANTIC_PAUSE_S = float(os.getenv("SEMANTIC_SEARCH_PAUSE_S", "60"))
_semantic_paused_until = 0.0


def semantic_first(question: str, ctx: Context, user) -> list:
    """Passages from vector search over the context's files, when the vector index is available.

    Circuit breaker: the search has a hard deadline (SEMANTIC_SEARCH_DEADLINE_S) in a daemon thread, so a vector
    store that is down or hanging never holds up an answer; after a failure, semantic search is skipped for
    SEMANTIC_SEARCH_PAUSE_S and keyword ranking is used."""
    global _semantic_paused_until
    if time.monotonic() < _semantic_paused_until:
        return []
    ids = {f.id for f in ctx.files}
    tenant = getattr(user, "client_id", None) or -1
    outcome = {}

    def work():
        try:
            from document_search import milvus_store
            from document_search.utils import embed_text
            outcome["hits"] = milvus_store.search(embed_text(question), ids, tenant, top_k=12)
        except Exception as exc:                      # noqa: BLE001 - any failure means keyword ranking
            outcome["error"] = exc

    worker = threading.Thread(target=work, name="semantic-search", daemon=True)
    worker.start()
    worker.join(SEMANTIC_DEADLINE_S)
    if worker.is_alive() or "error" in outcome:
        _semantic_paused_until = time.monotonic() + SEMANTIC_PAUSE_S
        logger.info("Semantic retrieval unavailable (%s); keyword ranking for %ss.",
                    outcome.get("error", "deadline exceeded"), int(SEMANTIC_PAUSE_S))
        try:
            from core import harness_runtime
            run = harness_runtime.current()
            if run is not None:
                run.note(step="semantic_unavailable")
        except Exception:
            pass
        return []
    by_id = {f.id: f for f in ctx.files}
    return [(by_id[int(h.entity.get("file_id"))], h.entity.get("chunk_text", ""))
            for h in outcome.get("hits") or [] if int(h.entity.get("file_id")) in by_id and h.entity.get("chunk_text")]


def build_sources(question: str, ctx: Context, user, *, semantic=True) -> list:
    """[{label, file_id, filename, excerpt}] filling MAX_CONTEXT_CHARS, most relevant first.
    semantic=False skips vector search (the AI evaluation's documents are not in the index)."""
    if not ctx.files:
        return []          # e.g. a data object with no documents behind it: nothing to search
    candidates = semantic_first(question, ctx, user) if semantic else []
    seen_text = {t[:200] for _, t in candidates}
    keyword = []
    for f in ctx.files:
        if ctx.kind == "documents" and f.content and len(f.content) > MAX_TEXT_PER_FILE:
            ctx.coverage_partial = True
        for piece in chunks_of(file_text(f)):
            if piece[:200] not in seen_text:
                keyword.append((f, piece))
    candidates += rank_passages(question, keyword)

    sources, used = [], 0
    for f, text in candidates:
        if used + len(text) > MAX_CONTEXT_CHARS:
            if used:
                break
            text = text[:MAX_CONTEXT_CHARS]
        sources.append({"label": f"S{len(sources) + 1}", "file_id": f.id, "filename": f.filename, "excerpt": text})
        used += len(text)
    if ctx.kind == "documents":
        ctx.coverage_partial |= (used < sum(len(t) for _, t in candidates)
                                 or {s["file_id"] for s in sources} != {f.pk for f in ctx.files})
    return sources


def assistant_model(user, provider=None, model=None) -> tuple[str, str]:
    """(provider, model) the assistant uses for this person: the request's choice, else the organisation's
    assistant policy, else the server defaults. Shared with the AI evaluation suite."""
    import os
    from core.llm_providers import default_provider
    from custom_authentication.policies import assistant_policy
    provider = str(provider or default_provider()).lower()
    model = (model or assistant_policy(user)["model"] or os.getenv("ASSISTANT_MODEL")
             or os.getenv("OPENAI_MODEL") or "gpt-4o-mini")
    return provider, model

