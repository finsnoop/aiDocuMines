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
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field

from django.http import Http404

from core.access import accessible_files, get_file_for_user, is_platform_superuser, is_tenant_admin, tenant_id
from core.models import File

logger = logging.getLogger(__name__)

MAX_FILES_SCANNED = 200          # documents read for one question
MAX_TEXT_PER_FILE = 200_000      # characters read per document
MAX_CONTEXT_CHARS = 14_000       # characters of passages sent to the model
CHUNK_SIZE = 1_200
CHUNK_OVERLAP = 150
MAX_FOLDERS = 5_000

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


# ── scope resolution ─────────────────────────────────────────────────────────

def _folder_ids_below(root_id) -> list:
    from document_operations.models import Folder

    ids, frontier = [root_id], [root_id]
    while frontier and len(ids) < MAX_FOLDERS:
        frontier = list(Folder.objects.filter(parent_id__in=frontier, is_trashed=False).values_list("id", flat=True))
        ids.extend(frontier)
    return ids[:MAX_FOLDERS]


def resolve_context(user, spec: dict) -> Context:
    """spec: {"type": file|folder|matter|project, "id": ..., "project_id": ..., "service_id": ...}."""
    kind = (spec or {}).get("type")
    readable = accessible_files(user).exclude(document_type="deliverable")

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
        owner_side = (folder.user_id == user.id or is_platform_superuser(user) or
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

    raise Http404("Choose a file, folder or matter")


def _bounded(ctx: Context, qs) -> Context:
    ctx.total_in_scope = qs.count()
    ctx.files = list(qs.order_by("-updated_at")[:MAX_FILES_SCANNED])
    return ctx


# ── text and passages ────────────────────────────────────────────────────────

def file_text(f: File) -> str:
    """Stored extracted text, or extract once and keep it (no save signals)."""
    if f.content:
        return f.content[:MAX_TEXT_PER_FILE]
    try:
        from document_search.utils import extract_text

        text = (extract_text(f.filepath) or "") if f.filepath else ""
    except Exception as exc:
        logger.info("Text extraction failed for file %s: %s", f.id, exc)
        text = ""
    if text:
        File.objects.filter(pk=f.pk).update(content=text)
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


def semantic_first(question: str, ctx: Context, user) -> list:
    """Passages from vector search over the context's files, when the vector index is available."""
    try:
        from document_search import milvus_store
        from document_search.utils import embed_text

        ids = {f.id for f in ctx.files}
        hits = milvus_store.search(embed_text(question), ids, getattr(user, "client_id", None) or -1, top_k=12)
        by_id = {f.id: f for f in ctx.files}
        return [(by_id[int(h.entity.get("file_id"))], h.entity.get("chunk_text", ""))
                for h in hits if int(h.entity.get("file_id")) in by_id and h.entity.get("chunk_text")]
    except Exception as exc:
        logger.debug("Semantic retrieval unavailable, using keyword ranking: %s", exc)
        return []


def build_sources(question: str, ctx: Context, user) -> list:
    """[{label, file_id, filename, excerpt}] filling MAX_CONTEXT_CHARS, most relevant first."""
    candidates = semantic_first(question, ctx, user)
    seen_text = {t[:200] for _, t in candidates}
    keyword = []
    for f in ctx.files:
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
    return sources


def compose_prompt(question: str, ctx: Context, sources: list, web: list) -> tuple[str, str]:
    """(query_text, context_block) for the model."""
    lines = [f"[{s['label']}] {s['filename']}\n{s['excerpt']}" for s in sources]
    lines += [f"[W{i + 1}] {w['title']} ({w['url']})\n{w['snippet']}" for i, w in enumerate(web)]
    rules = (
        f"You are answering a question about the {ctx.kind} \"{ctx.label}\". "
        "Use the numbered sources below. Cite every statement with its source label, for example [S1] or [W2]. "
        + ("Sources starting with W are public web results; say clearly when a point comes from the web. "
           if web else "Do not use outside knowledge; if the sources do not answer the question, say so. ")
        + "Be precise and concise."
    )
    return f"{rules}\n\nQuestion: {question}", "\n\n".join(lines) or "(No readable text was found in these documents.)"
