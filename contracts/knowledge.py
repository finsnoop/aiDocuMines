"""
Playbooks, benchmarks and deviation / compliance analysis: /api/v1/knowledge/ (switch: "Playbooks & benchmarks").

One organisation, always: every lookup is filtered by the caller's organisation, so another organisation's IDs are
"not found". Roles (custom_authentication/rbac.py):
  playbook.view / benchmark.view       see the library and pick them when checking a document (standard users)
  playbook.create|update|delete|upload|export / benchmark.manage   change them (managers and administrators)
  knowledge.analyze                     run checks on documents the person may process
Documents are reached only through core.access (organisation, roles, sharing, matter teams, ethical walls, holds).

Playbooks
  GET    playbooks/?search=&category=&tag=&industry=&document_type=&use_case=&active=&sort=&order=&limit=&offset=
  POST   playbooks/                        {name, description, content, category, tags[], industry, document_type,
                                            use_case, contract_type, active}
  POST   playbooks/upload/                 multipart: file + the fields above (the file goes through core/ingest.py)
  GET|PATCH|DELETE playbooks/<id>/
  GET    playbooks/<id>/export/?as=json|md
  GET    playbooks/<id>/file/              the uploaded document (watermarked when the organisation requires it)
  POST   playbooks/<id>/positions/          PATCH|DELETE playbooks/<id>/positions/<pid>/
  PUT    playbooks/<id>/benchmarks/        {benchmark_ids: [...]}    the benchmarks this playbook relies on
  GET    tags/                             tags in use in the organisation
Benchmarks
  GET|POST benchmarks/                     {name, kind, description, file_id?, category, tags[], version_label,
                                            effective_date, review_date, active}
  GET|PATCH|DELETE benchmarks/<id>/
  POST   benchmarks/<id>/terms/             PATCH|DELETE benchmarks/<id>/terms/<tid>/
  POST   benchmarks/<id>/suggest-terms/    AI proposals from the benchmark document (verbatim quotes; not saved)
  POST   benchmarks/from-document/         {file_id, name?, kind?, bucket_id? | new_bucket?}  "Set as benchmark": freezes the
                                            document as it is now (own bytes, version and SHA-256 recorded, read-only)
  GET    benchmarks/tree/                  Category -> matter (project_id) -> service (service_id) -> benchmarks
  GET|POST categories/                      PATCH|DELETE categories/<id>/   (buckets; removing one keeps its benchmarks)
Links (a playbook or benchmark applied to a contract, matter or document)
  GET    links/?contract_id=|matter_id=|file_id=      POST links/ {playbook_id|benchmark_id, contract_id|matter_id|file_id}
  DELETE links/<id>/
  GET    applicable/?file_id=|contract_id=|matter_id=  linked (directly, via the contract, via the matter) + all active
Analyses
  POST   analyses/                         {file_id | contract_id, playbook_id?, benchmark_ids?[]}
  POST   analyses/matter/                  {matter_id, playbook_id?, benchmark_ids?[]}   every document in the matter (max 50)
  GET    analyses/?file_id=|contract_id=|matter_id=       GET analyses/<id>/
"""
from __future__ import annotations

import json
import re
from datetime import date

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponse
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import can_access_file, get_file_for_user, matters_for_user, tenant_id, user_has_perm
from core.models import File, Matter
from . import access as contract_access
from .models import (Benchmark, BenchmarkCategory, BenchmarkTerm, Contract, KnowledgeLink, Playbook, PlaybookPosition,
                     PlaybookReview)

MAX_POSITIONS = 60
MAX_TERMS = 80
MAX_MATTER_DOCUMENTS = 50
SORTS = {"name": "name", "createdAt": "created_at", "updatedAt": "updated_at", "created_at": "created_at",
         "updated_at": "updated_at"}
TAG_RE = re.compile(r"^[\w .&/-]{1,60}$")


# ── helpers ─────────────────────────────────────────────────────────────────────────────────
def _err(msg, status=400):
    return Response({"error": msg}, status=status)


def _bool(v):
    return v.strip().lower() in ("1", "true", "yes", "on") if isinstance(v, str) else bool(v)


def _text(v, limit):
    return str(v or "").strip()[:limit]


def _tags(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = [t for t in v.split(",")]
    if not isinstance(v, list):
        return []
    out = []
    for t in v:
        t = str(t).strip()
        if t and TAG_RE.match(t) and t not in out:
            out.append(t)
    return out[:20]


def _date(v):
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise ValueError("Use dates in the form YYYY-MM-DD.")


def _dec(v):
    from .benchmarking import _dec as parse
    if v in (None, ""):
        return None
    d = parse(v)
    if d is None:
        raise ValueError("Numbers must be plain numbers, for example 99.9 or 30.")
    return d


def can_view_playbooks(user):
    return user_has_perm(user, "playbook.view", "playbook.create", "contract.manage") or contract_access.is_manager(user)


def can_manage_playbooks(user):
    return user_has_perm(user, "playbook.create", "playbook.update") or contract_access.is_manager(user)


def can_view_benchmarks(user):
    return user_has_perm(user, "benchmark.view", "benchmark.manage") or contract_access.is_manager(user)


def can_manage_benchmarks(user):
    return user_has_perm(user, "benchmark.manage") or contract_access.is_manager(user)


def _uuid_ok(pk):
    return bool(re.fullmatch(r"[0-9a-fA-F-]{32,36}", str(pk or "")))


def _playbook(user, pk):
    tid = tenant_id(user)
    return Playbook.objects.filter(pk=pk, client_id=tid).first() if tid and _uuid_ok(pk) else None


def _benchmark(user, pk):
    tid = tenant_id(user)
    return Benchmark.objects.filter(pk=pk, client_id=tid).first() if tid and _uuid_ok(pk) else None


def _benchmarks(user, ids):
    ids = [i for i in (ids if isinstance(ids, list) else []) if _uuid_ok(i)][:20]
    found = list(Benchmark.objects.filter(pk__in=ids, client_id=tenant_id(user)))
    if len(found) != len(set(ids)):
        raise LookupError("One or more benchmarks were not found.")
    return found


def _file(user, file_id, action="view"):
    """A document the user may reach, or None (another organisation, walls, no access: all 'not found')."""
    if not str(file_id or "").isdigit():
        return None
    try:
        f = get_file_for_user(user, int(file_id), action)
    except Exception:
        return None
    return f if f is not None and getattr(f.user, "client_id", None) == tenant_id(user) else None


def _contract(user, pk):
    if not _uuid_ok(pk):
        return None
    return contract_access.contracts_for(user).filter(pk=pk).first()


def _matter(user, pk):
    if not _uuid_ok(pk):
        return None
    return matters_for_user(user).filter(pk=pk, client_id=tenant_id(user)).first()


def matter_of_file(f):
    """The matter a document belongs to (same organisation, matter name = the document's project)."""
    client_id = getattr(f.user, "client_id", None)
    return Matter.objects.filter(client_id=client_id, name=f.project_id).first() if client_id else None


# ── serialisers ─────────────────────────────────────────────────────────────────────────────
def position_out(p):
    return {"id": p.pk, "topic": p.topic, "standard": p.standard, "fallbacks": p.fallbacks, "red_lines": p.red_lines,
            "guidance": p.guidance, "severity": p.severity, "order": p.order}


def term_out(t):
    num = lambda v: None if v is None else float(v)   # noqa: E731
    return {"id": t.pk, "topic": t.topic, "requirement": t.requirement, "comparator": t.comparator,
            "comparator_label": t.get_comparator_display(), "target_value": num(t.target_value),
            "target_max": num(t.target_max), "unit": t.unit, "tolerance": num(t.tolerance), "severity": t.severity,
            "source_quote": t.source_quote, "order": t.order}


def playbook_out(pb, full=False):
    data = {"id": str(pb.id), "name": pb.name, "description": pb.description, "category": pb.category,
            "categoryId": pb.category, "tags": pb.tags, "industry": pb.industry, "document_type": pb.document_type,
            "documentType": pb.document_type, "use_case": pb.use_case, "useCase": pb.use_case,
            "contract_type": pb.contract_type, "active": pb.active, "version": pb.version,
            "position_count": pb.positions.count(), "benchmark_ids": [str(b) for b in pb.benchmarks.values_list("id", flat=True)],
            "has_file": bool(pb.source_file_id), "file_name": pb.source_file.filename if pb.source_file_id else None,
            "created_by": getattr(pb.created_by, "email", None), "created_at": pb.created_at, "updated_at": pb.updated_at,
            "metadata": pb.metadata}
    if full:
        data["content"] = pb.content
        data["positions"] = [position_out(p) for p in pb.positions.all()]
        data["benchmarks"] = [{"id": str(b.id), "name": b.name, "kind": b.kind} for b in pb.benchmarks.all()]
    return data


def benchmark_out(b, full=False):
    data = {"id": str(b.id), "name": b.name, "kind": b.kind, "kind_label": b.get_kind_display(),
            "description": b.description, "category": b.category, "tags": b.tags, "version_label": b.version_label,
            "effective_date": b.effective_date, "review_date": b.review_date, "active": b.active,
            "file": {"id": b.file_id, "filename": b.file.filename} if b.file_id else None,
            "term_count": b.terms.count(), "created_at": b.created_at, "updated_at": b.updated_at,
            "bucket": {"id": str(b.bucket_id), "name": b.bucket.name} if b.bucket_id else None,
            "source": ({"file_id": b.source_file_id, "version": b.source_version, "project_id": b.source_project_id,
                        "service_id": b.source_service_id, "snapshot_at": b.snapshot_at, "sha256": b.snapshot_sha256,
                        "original_available": b.source_file_id is not None} if b.snapshot_at else None)}
    if full:
        data["terms"] = [term_out(t) for t in b.terms.all()]
        data["playbooks"] = [{"id": str(p.id), "name": p.name} for p in b.playbooks.all()]
    return data


def link_out(link):
    source = ({"type": "playbook", "id": str(link.playbook_id), "name": link.playbook.name} if link.playbook_id
              else {"type": "benchmark", "id": str(link.benchmark_id), "name": link.benchmark.name})
    target = ({"type": "contract", "id": str(link.contract_id)} if link.contract_id
              else {"type": "matter", "id": str(link.matter_id)} if link.matter_id
              else {"type": "file", "id": link.file_id})
    return {"id": link.pk, "source": source, "target": target, "created_at": link.created_at}


def review_out(r, findings=True):
    num = lambda v: None if v is None else float(v)   # noqa: E731
    data = {"id": str(r.id), "status": r.status, "error": r.error, "summary": r.summary, "created_at": r.created_at,
            "finished_at": r.finished_at,
            "playbook": {"id": str(r.playbook_id), "name": r.playbook.name} if r.playbook_id else None,
            "benchmarks": [{"id": str(b.id), "name": b.name} for b in r.benchmarks.all()],
            "file": {"id": r.file_id, "filename": r.file.filename} if r.file_id else None,
            "contract": {"id": str(r.contract_id), "title": r.contract.title} if r.contract_id else None,
            "matter": {"id": str(r.matter_id), "name": r.matter.name} if r.matter_id else None}
    if findings:
        data["findings"] = [{
            "id": f.pk, "topic": f.topic, "severity": f.severity, "status": f.status,
            "status_label": "Breaches the benchmark" if f.benchmark_term_id and f.status == "red_line" else f.get_status_display(),
            "source": ({"type": "position", "id": f.position_id} if f.position_id else
                       {"type": "benchmark_term", "id": f.benchmark_term_id} if f.benchmark_term_id else None),
            "quote": f.quote, "offset": f.offset, "explanation": f.explanation, "proposed_text": f.proposed_text,
            "measured_value": num(f.measured_value), "gap": num(f.gap), "unit": f.unit,
            "confidence": round(f.confidence, 2)} for f in r.findings.all()]
    return data


# ── playbooks ───────────────────────────────────────────────────────────────────────────────
PLAYBOOK_FIELDS = {"description": 20000, "content": 500000, "category": 60, "industry": 100, "document_type": 100,
                   "use_case": 100, "contract_type": 60}
# The Playbooks page sends both styles (camelCase JSON, snake_case form fields from the old Node API).
ALIASES = {"categoryId": "category", "category_id": "category", "documentType": "document_type",
           "useCase": "use_case", "tagIds": "tags", "tag_ids": "tags"}


def _apply_playbook(pb, data):
    data = {ALIASES.get(k, k): v for k, v in data.items()}
    if "name" in data:
        name = _text(data["name"], 255)
        if not name:
            raise ValueError("Give the playbook a name.")
        pb.name = name
    for field, limit in PLAYBOOK_FIELDS.items():
        if field in data:
            setattr(pb, field, str(data[field] or "")[:limit])
    if "tags" in data:
        pb.tags = _tags(data["tags"])
    if "active" in data:
        pb.active = _bool(data["active"])
    if "metadata" in data and isinstance(data["metadata"], dict):
        pb.metadata = {k: v for k, v in data["metadata"].items() if isinstance(k, str)}
    if not pb.name:
        raise ValueError("Give the playbook a name.")


class PlaybooksView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not can_view_playbooks(request.user):
            return _err("You do not have access to playbooks.", 403)
        q = request.query_params
        rows = Playbook.objects.filter(client_id=tenant_id(request.user)).select_related("source_file", "created_by")
        if q.get("search"):
            rows = rows.filter(Q(name__icontains=q["search"]) | Q(description__icontains=q["search"]))
        for param, field in (("category", "category"), ("category_id", "category"), ("industry", "industry"),
                             ("document_type", "document_type"), ("use_case", "use_case"), ("contract_type", "contract_type")):
            if q.get(param):
                rows = rows.filter(**{field: q[param]})
        if q.get("active") in ("1", "true"):
            rows = rows.filter(active=True)
        tags = _tags(q.get("tag") or q.get("tag_ids") or [])
        if tags:
            rows = rows.filter(tags__contains=[tags[0]]) if len(tags) == 1 else rows.filter(
                Q(*[Q(tags__contains=[t]) for t in tags], _connector=Q.OR))
        sort = SORTS.get(q.get("sort") or q.get("sort_by") or "", "updated_at")
        rows = rows.order_by(sort if (q.get("order") or q.get("sort_order") or "desc") == "asc" else f"-{sort}")
        total = rows.count()
        limit = min(max(int(q.get("limit") or 50) if str(q.get("limit") or "50").isdigit() else 50, 1), 500)
        offset = int(q.get("offset") or 0) if str(q.get("offset") or "0").isdigit() else 0
        return Response({"results": [playbook_out(p) for p in rows[offset:offset + limit]], "total": total,
                         "limit": limit, "offset": offset, "can_manage": can_manage_playbooks(request.user)})

    def post(self, request):
        if not can_manage_playbooks(request.user):
            return _err("Only managers and administrators can create playbooks.", 403)
        pb = Playbook(client_id=tenant_id(request.user), created_by=request.user, updated_by=request.user)
        try:
            _apply_playbook(pb, request.data)
        except ValueError as exc:
            return _err(str(exc))
        pb.save()
        return Response(playbook_out(pb, full=True), status=201)


def knowledge_matter(user):
    """The organisation's matter that holds playbook and benchmark documents (created on first use)."""
    matter, _ = Matter.objects.get_or_create(client_id=tenant_id(user), name="Knowledge library",
                                             defaults={"created_by": user, "practice_area": "Knowledge",
                                                       "description": "Playbook and benchmark documents."})
    matter.members.add(user)
    return matter


def ingest_upload(user, upload):
    """Store an uploaded file through the shared ingestion pipeline; returns the File."""
    from core.ingest import MatterBatch
    batch = MatterBatch(user, knowledge_matter(user), source="knowledge")
    try:
        entry = batch.add_stream(upload.name, upload.chunks())
    finally:
        batch.finish()
    if entry.get("status") == "rejected" or not entry.get("file_id"):
        raise ValueError(entry.get("message") or "The file could not be stored.")
    return File.objects.get(pk=entry["file_id"])


class PlaybookUploadView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        if not (can_manage_playbooks(request.user) and (user_has_perm(request.user, "playbook.upload")
                                                         or contract_access.is_manager(request.user))):
            return _err("Only managers and administrators can upload playbooks.", 403)
        upload = request.FILES.get("file")
        if upload is None:
            return _err("Choose a file to upload.")
        pb = Playbook(client_id=tenant_id(request.user), created_by=request.user, updated_by=request.user)
        data = {k: v for k, v in request.data.items() if k != "file"}
        data.setdefault("name", upload.name)
        try:
            _apply_playbook(pb, data)
            with transaction.atomic():
                pb.source_file = ingest_upload(request.user, upload)
                pb.save()
        except ValueError as exc:
            return _err(str(exc))
        return Response(playbook_out(pb, full=True), status=201)


class PlaybookView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        pb = _playbook(request.user, pk) if can_view_playbooks(request.user) else None
        return Response(playbook_out(pb, full=True)) if pb else _err("Playbook not found.", 404)

    def patch(self, request, pk):
        pb = _playbook(request.user, pk) if can_view_playbooks(request.user) else None
        if pb is None:
            return _err("Playbook not found.", 404)
        if not can_manage_playbooks(request.user):
            return _err("Only managers and administrators can change playbooks.", 403)
        try:
            _apply_playbook(pb, request.data)
        except ValueError as exc:
            return _err(str(exc))
        pb.version += 1
        pb.updated_by = request.user
        pb.save()
        return Response(playbook_out(pb, full=True))

    put = patch

    def delete(self, request, pk):
        pb = _playbook(request.user, pk) if can_view_playbooks(request.user) else None
        if pb is None:
            return _err("Playbook not found.", 404)
        if not (user_has_perm(request.user, "playbook.delete") or contract_access.is_manager(request.user)):
            return _err("Only managers and administrators can remove playbooks.", 403)
        pb.delete()          # reviews keep their findings (playbook set to null)
        return Response(status=204)


class PlaybookExportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        pb = _playbook(request.user, pk) if can_view_playbooks(request.user) else None
        if pb is None:
            return _err("Playbook not found.", 404)
        data = playbook_out(pb, full=True)
        safe = re.sub(r"[^\w.-]+", "_", pb.name)[:80] or "playbook"
        if request.query_params.get("as") == "md":         # not ?format=: DRF reserves it for renderers
            lines = [f"# {pb.name}", "", pb.description, "", pb.content, ""]
            for p in data["positions"]:
                lines += [f"## {p['topic']} ({p['severity']})", "", f"**Standard:** {p['standard']}"]
                lines += [f"- Fallback: {f}" for f in p["fallbacks"]] + [f"- Red line: {r}" for r in p["red_lines"]]
                lines += [f"_Guidance:_ {p['guidance']}", ""] if p["guidance"] else [""]
            body, ctype, ext = "\n".join(lines), "text/markdown; charset=utf-8", "md"
        else:
            body, ctype, ext = json.dumps(data, indent=2, default=str), "application/json", "json"
        response = HttpResponse(body, content_type=ctype)
        response["Content-Disposition"] = f'attachment; filename="{safe}.{ext}"'
        response["Cache-Control"] = "private, no-store"
        return response


class PlaybookFileView(APIView):
    """The playbook's uploaded document: library material for everyone in the organisation who may view
    playbooks (watermarked with the viewer when the organisation requires it)."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        import os
        from core.file_responses import download_response
        from core.watermark import text_for
        pb = _playbook(request.user, pk) if can_view_playbooks(request.user) else None
        f = pb.source_file if pb else None
        if f is None or not f.filepath or not os.path.isfile(f.filepath):
            return _err("No file for this playbook.", 404)
        mark = text_for(pb.client_id, request.user)
        return download_response(f.filepath, f.filename, inline=request.query_params.get("inline") == "true",
                                 watermark=mark, owner_client_id=pb.client_id)


def _str_list(value, limit=10):
    return [str(v).strip()[:2000] for v in value if str(v).strip()][:limit] if isinstance(value, list) else []


class PositionsView(APIView):
    permission_classes = [IsAuthenticated]

    def _guard(self, request, pk):
        pb = _playbook(request.user, pk) if can_view_playbooks(request.user) else None
        if pb is None:
            return None, _err("Playbook not found.", 404)
        if not can_manage_playbooks(request.user):
            return None, _err("Only managers and administrators can change playbooks.", 403)
        return pb, None

    def _apply(self, p, data):
        if "topic" in data:
            p.topic = _text(data["topic"], 120)
        if "standard" in data:
            p.standard = str(data["standard"] or "")[:8000]
        for f in ("fallbacks", "red_lines"):
            if f in data:
                setattr(p, f, _str_list(data[f]))
        if "guidance" in data:
            p.guidance = str(data["guidance"] or "")[:4000]
        if data.get("severity") in ("low", "medium", "high"):
            p.severity = data["severity"]
        if str(data.get("order") or "").isdigit():
            p.order = int(data["order"])
        if not p.topic or not p.standard.strip():
            raise ValueError("A position needs a topic and the preferred position.")

    def post(self, request, pk):
        pb, error = self._guard(request, pk)
        if error:
            return error
        if pb.positions.count() >= MAX_POSITIONS:
            return _err(f"A playbook can have at most {MAX_POSITIONS} positions.")
        p = PlaybookPosition(playbook=pb, order=pb.positions.count() + 1)
        try:
            self._apply(p, request.data)
        except ValueError as exc:
            return _err(str(exc))
        p.save()
        Playbook.objects.filter(pk=pb.pk).update(version=pb.version + 1, updated_by=request.user)
        return Response(position_out(p), status=201)

    def patch(self, request, pk, position_id):
        pb, error = self._guard(request, pk)
        if error:
            return error
        p = pb.positions.filter(pk=position_id).first()
        if p is None:
            return _err("Position not found.", 404)
        try:
            self._apply(p, request.data)
        except ValueError as exc:
            return _err(str(exc))
        p.save()
        return Response(position_out(p))

    def delete(self, request, pk, position_id):
        pb, error = self._guard(request, pk)
        if error:
            return error
        deleted, _ = pb.positions.filter(pk=position_id).delete()
        return Response(status=204) if deleted else _err("Position not found.", 404)


class PlaybookBenchmarksView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request, pk):
        pb = _playbook(request.user, pk) if can_view_playbooks(request.user) else None
        if pb is None:
            return _err("Playbook not found.", 404)
        if not can_manage_playbooks(request.user):
            return _err("Only managers and administrators can change playbooks.", 403)
        try:
            pb.benchmarks.set(_benchmarks(request.user, request.data.get("benchmark_ids")))
        except LookupError as exc:
            return _err(str(exc), 404)
        return Response(playbook_out(pb, full=True))


class TagsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tid = tenant_id(request.user)
        used = set()
        for tags in list(Playbook.objects.filter(client_id=tid).values_list("tags", flat=True)) + \
                list(Benchmark.objects.filter(client_id=tid).values_list("tags", flat=True)):
            used.update(t for t in (tags or []) if isinstance(t, str))
        return Response([{"id": t, "name": t} for t in sorted(used, key=str.lower)])


# ── benchmarks ──────────────────────────────────────────────────────────────────────────────
def _apply_benchmark(user, b, data):
    if "name" in data:
        b.name = _text(data["name"], 255)
    if data.get("kind") in {k for k, _ in Benchmark.KINDS}:
        b.kind = data["kind"]
    elif "kind" in data:
        raise ValueError("Choose a benchmark type: policy, SLA, master agreement, standard terms, regulation, "
                         "template, precedent or other.")
    for field, limit in (("description", 20000), ("category", 60), ("version_label", 40)):
        if field in data:
            setattr(b, field, str(data[field] or "")[:limit])
    if "tags" in data:
        b.tags = _tags(data["tags"])
    for field in ("effective_date", "review_date"):
        if field in data:
            setattr(b, field, _date(data[field]))
    if "active" in data:
        b.active = _bool(data["active"])
    if "bucket_id" in data:
        if data["bucket_id"] in (None, ""):
            b.bucket = None
        else:
            bucket = _bucket(user, data["bucket_id"])
            if bucket is None:
                raise LookupError("That category was not found.")
            b.bucket = bucket
    if "file_id" in data:
        if data["file_id"] in (None, ""):
            b.file = None
        else:
            f = _file(user, data["file_id"], "view")
            if f is None:
                raise LookupError("That document was not found.")
            b.file = f
    if not b.name:
        raise ValueError("Give the benchmark a name.")


class BenchmarksView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get(self, request):
        if not can_view_benchmarks(request.user):
            return _err("You do not have access to benchmarks.", 403)
        q = request.query_params
        rows = Benchmark.objects.filter(client_id=tenant_id(request.user)).select_related("file")
        if q.get("kind"):
            rows = rows.filter(kind=q["kind"])
        if q.get("search"):
            rows = rows.filter(Q(name__icontains=q["search"]) | Q(description__icontains=q["search"]))
        if q.get("active") in ("1", "true"):
            rows = rows.filter(active=True)
        return Response({"results": [benchmark_out(b) for b in rows[:500]], "kinds": [{"id": k, "name": n} for k, n in Benchmark.KINDS],
                         "can_manage": can_manage_benchmarks(request.user)})

    def post(self, request):
        if not can_manage_benchmarks(request.user):
            return _err("Only managers and administrators can create benchmarks.", 403)
        b = Benchmark(client_id=tenant_id(request.user), created_by=request.user)
        data = {k: v for k, v in request.data.items() if k != "file"}
        try:
            with transaction.atomic():
                if request.FILES.get("file"):
                    data.setdefault("name", request.FILES["file"].name)
                    b.file = ingest_upload(request.user, request.FILES["file"])
                    data.pop("file_id", None)
                _apply_benchmark(request.user, b, data)
                b.save()
        except LookupError as exc:
            return _err(str(exc), 404)
        except ValueError as exc:
            return _err(str(exc))
        return Response(benchmark_out(b, full=True), status=201)


class BenchmarkView(APIView):
    permission_classes = [IsAuthenticated]

    def _get(self, request, pk):
        return _benchmark(request.user, pk) if can_view_benchmarks(request.user) else None

    def get(self, request, pk):
        b = self._get(request, pk)
        return Response(benchmark_out(b, full=True)) if b else _err("Benchmark not found.", 404)

    def patch(self, request, pk):
        b = self._get(request, pk)
        if b is None:
            return _err("Benchmark not found.", 404)
        if not can_manage_benchmarks(request.user):
            return _err("Only managers and administrators can change benchmarks.", 403)
        try:
            _apply_benchmark(request.user, b, request.data)
        except LookupError as exc:
            return _err(str(exc), 404)
        except ValueError as exc:
            return _err(str(exc))
        b.save()
        return Response(benchmark_out(b, full=True))

    def delete(self, request, pk):
        b = self._get(request, pk)
        if b is None:
            return _err("Benchmark not found.", 404)
        if not can_manage_benchmarks(request.user):
            return _err("Only managers and administrators can remove benchmarks.", 403)
        b.delete()           # the reference document stays in the document system
        return Response(status=204)


def _apply_term(t, data):
    if "topic" in data:
        t.topic = _text(data["topic"], 120)
    if "requirement" in data:
        t.requirement = str(data["requirement"] or "")[:4000]
    if "comparator" in data:
        if data["comparator"] not in {c for c, _ in BenchmarkTerm.COMPARATORS}:
            raise ValueError("Choose how the value is compared: at least, at most, exactly, between, present or absent.")
        t.comparator = data["comparator"]
    for field in ("target_value", "target_max", "tolerance"):
        if field in data:
            setattr(t, field, _dec(data[field]))
    if "unit" in data:
        t.unit = _text(data["unit"], 20)
    if data.get("severity") in ("low", "medium", "high"):
        t.severity = data["severity"]
    if "source_quote" in data:
        t.source_quote = str(data["source_quote"] or "")[:4000]
    if str(data.get("order") or "").isdigit():
        t.order = int(data["order"])
    if not t.topic or not t.requirement.strip():
        raise ValueError("A term needs a topic and the requirement in words.")
    if t.comparator in (">=", "<=", "=") and t.target_value is None:
        raise ValueError("Give the target number for this term.")
    if t.comparator == "between" and (t.target_value is None or t.target_max is None or t.target_value > t.target_max):
        raise ValueError("Give the lowest and highest acceptable values.")
    if t.tolerance is not None and t.tolerance < 0:
        raise ValueError("Tolerance cannot be negative.")


class TermsView(APIView):
    permission_classes = [IsAuthenticated]

    def _guard(self, request, pk):
        b = _benchmark(request.user, pk) if can_view_benchmarks(request.user) else None
        if b is None:
            return None, _err("Benchmark not found.", 404)
        if not can_manage_benchmarks(request.user):
            return None, _err("Only managers and administrators can change benchmarks.", 403)
        return b, None

    def post(self, request, pk):
        b, error = self._guard(request, pk)
        if error:
            return error
        if b.terms.count() >= MAX_TERMS:
            return _err(f"A benchmark can have at most {MAX_TERMS} terms.")
        t = BenchmarkTerm(benchmark=b, order=b.terms.count() + 1)
        try:
            _apply_term(t, request.data)
        except ValueError as exc:
            return _err(str(exc))
        t.save()
        return Response(term_out(t), status=201)

    def patch(self, request, pk, term_id):
        b, error = self._guard(request, pk)
        if error:
            return error
        t = b.terms.filter(pk=term_id).first()
        if t is None:
            return _err("Term not found.", 404)
        try:
            _apply_term(t, request.data)
        except ValueError as exc:
            return _err(str(exc))
        t.save()
        return Response(term_out(t))

    def delete(self, request, pk, term_id):
        b, error = self._guard(request, pk)
        if error:
            return error
        deleted, _ = b.terms.filter(pk=term_id).delete()
        return Response(status=204) if deleted else _err("Term not found.", 404)


class SuggestTermsView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        from .benchmarking import suggest_terms
        from .playbook import document_text
        b = _benchmark(request.user, pk) if can_view_benchmarks(request.user) else None
        if b is None:
            return _err("Benchmark not found.", 404)
        if not can_manage_benchmarks(request.user):
            return _err("Only managers and administrators can change benchmarks.", 403)
        if b.file is None or not can_access_file(request.user, b.file, "process"):
            return _err("Attach the benchmark document (one you may process) first.")
        text = document_text(b.file)
        if not text.strip():
            return _err("No text could be read from the benchmark document.")
        try:
            return Response({"suggestions": suggest_terms(request.user, text)})
        except ValueError as exc:        # AI limits, organisation policy
            return _err(str(exc), 429)


# ── links ───────────────────────────────────────────────────────────────────────────────────
def _target(request, data, for_change=False):
    """(field, object) for contract_id / matter_id / file_id, checked against the caller's access."""
    present = [k for k in ("contract_id", "matter_id", "file_id") if data.get(k) not in (None, "")]
    if len(present) != 1:
        raise ValueError("Choose one contract, matter or document.")
    key = present[0]
    if key == "contract_id":
        obj = _contract(request.user, data[key])
        if obj is not None and for_change and not contract_access.can_edit(request.user, obj):
            raise PermissionError("Only the contract's owner or a contract manager can change what applies to it.")
    elif key == "matter_id":
        obj = _matter(request.user, data[key])
    else:
        obj = _file(request.user, data[key], "view")
    if obj is None:
        raise LookupError("That contract, matter or document was not found.")
    return key[:-3], obj


class LinksView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            field, obj = _target(request, request.query_params)
        except (ValueError, LookupError, PermissionError) as exc:
            return _err(str(exc), 404 if isinstance(exc, LookupError) else 400)
        links = KnowledgeLink.objects.filter(client_id=tenant_id(request.user), **{field: obj}) \
            .select_related("playbook", "benchmark")
        return Response({"results": [link_out(link) for link in links]})

    def post(self, request):
        data = request.data
        if bool(data.get("playbook_id")) == bool(data.get("benchmark_id")):
            return _err("Choose a playbook or a benchmark.")
        if data.get("playbook_id"):
            if not can_manage_playbooks(request.user):
                return _err("Only managers and administrators can apply playbooks.", 403)
            source = {"playbook": _playbook(request.user, data["playbook_id"])}
        else:
            if not can_manage_benchmarks(request.user):
                return _err("Only managers and administrators can apply benchmarks.", 403)
            source = {"benchmark": _benchmark(request.user, data["benchmark_id"])}
        if None in source.values():
            return _err("Playbook or benchmark not found.", 404)
        try:
            field, obj = _target(request, data, for_change=True)
        except LookupError as exc:
            return _err(str(exc), 404)
        except PermissionError as exc:
            return _err(str(exc), 403)
        except ValueError as exc:
            return _err(str(exc))
        try:
            link, created = KnowledgeLink.objects.get_or_create(client_id=tenant_id(request.user), **source, **{field: obj},
                                                                defaults={"created_by": request.user})
        except IntegrityError:
            return _err("This is already applied.", 409)
        return Response(link_out(link), status=201 if created else 200)


class LinkView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk):
        link = KnowledgeLink.objects.filter(pk=pk, client_id=tenant_id(request.user)).select_related("contract").first() \
            if str(pk).isdigit() else None
        if link is None:
            return _err("Link not found.", 404)
        if not (can_manage_playbooks(request.user) if link.playbook_id else can_manage_benchmarks(request.user)):
            return _err("Only managers and administrators can change what applies.", 403)
        if link.contract_id and not contract_access.can_edit(request.user, link.contract):
            return _err("Only the contract's owner or a contract manager can change what applies to it.", 403)
        link.delete()
        return Response(status=204)


def applicable(user, *, file=None, contract=None, matter=None) -> dict:
    """Playbooks and benchmarks linked to the object (directly, through its contract, through its matter),
    then every other active one of the organisation the user may see."""
    tid = tenant_id(user)
    scopes = Q(pk__in=[])
    if file is not None:
        scopes |= Q(file=file)
        contract_ids = list(Contract.objects.filter(client_id=tid, document=file).values_list("pk", flat=True))
        scopes |= Q(contract_id__in=contract_ids)
        matter = matter or matter_of_file(file)
    if contract is not None:
        scopes |= Q(contract=contract)
        if contract.document_id:
            scopes |= Q(file_id=contract.document_id)
        matter = matter or contract.matter
    if matter is not None and matter.client_id == tid:
        scopes |= Q(matter=matter)
    links = KnowledgeLink.objects.filter(scopes, client_id=tid)
    linked_pb = set(links.exclude(playbook=None).values_list("playbook_id", flat=True))
    linked_bm = set(links.exclude(benchmark=None).values_list("benchmark_id", flat=True))
    # A linked playbook brings the benchmarks it relies on.
    linked_bm |= set(Benchmark.objects.filter(client_id=tid, playbooks__in=linked_pb).values_list("pk", flat=True))
    out = {"linked": {"playbooks": [], "benchmarks": []}, "other": {"playbooks": [], "benchmarks": []},
           "matter": {"id": str(matter.id), "name": matter.name} if matter is not None else None}
    if can_view_playbooks(user):
        for pb in Playbook.objects.filter(client_id=tid, active=True):
            (out["linked"] if pb.pk in linked_pb else out["other"])["playbooks"].append(
                {"id": str(pb.id), "name": pb.name, "position_count": pb.positions.count(), "contract_type": pb.contract_type})
    if can_view_benchmarks(user):
        for b in Benchmark.objects.filter(client_id=tid, active=True):
            (out["linked"] if b.pk in linked_bm else out["other"])["benchmarks"].append(
                {"id": str(b.id), "name": b.name, "kind": b.kind, "kind_label": b.get_kind_display(), "term_count": b.terms.count()})
    return out


class ApplicableView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        q = request.query_params
        file = contract = matter = None
        if q.get("file_id"):
            file = _file(request.user, q["file_id"], "view")
        elif q.get("contract_id"):
            contract = _contract(request.user, q["contract_id"])
        elif q.get("matter_id"):
            matter = _matter(request.user, q["matter_id"])
        if file is None and contract is None and matter is None:
            return _err("That document, contract or matter was not found.", 404)
        return Response(applicable(request.user, file=file, contract=contract, matter=matter))


# ── analyses ────────────────────────────────────────────────────────────────────────────────
def _sources(request):
    """(playbook or None, [benchmarks]) from the request, or an error Response."""
    pb, benchmarks = None, []
    if request.data.get("playbook_id"):
        pb = _playbook(request.user, request.data["playbook_id"]) if can_view_playbooks(request.user) else None
        if pb is None:
            return None, None, _err("Playbook not found.", 404)
    if request.data.get("benchmark_ids"):
        if not can_view_benchmarks(request.user):
            return None, None, _err("Benchmark not found.", 404)
        try:
            benchmarks = _benchmarks(request.user, request.data["benchmark_ids"])
        except LookupError as exc:
            return None, None, _err(str(exc), 404)
    if pb is None and not benchmarks:
        return None, None, _err("Choose a playbook, one or more benchmarks, or both.")
    if pb is not None and not pb.positions.exists() and not benchmarks:
        return None, None, _err("This playbook has no positions yet. Add positions or choose a benchmark.")
    if benchmarks and not any(b.terms.exists() for b in benchmarks) and pb is None:
        return None, None, _err("These benchmarks have no terms yet. Add terms first.")
    return pb, benchmarks, None


def queue_review(user, file, pb, benchmarks, contract=None, matter=None):
    from .playbook import playbook_review_task
    review = PlaybookReview.objects.create(client_id=tenant_id(user), file=file, contract=contract, playbook=pb,
                                           matter=matter or matter_of_file(file), requested_by=user)
    if benchmarks:
        review.benchmarks.set(benchmarks)
    playbook_review_task.delay(str(review.pk), user_id=user.pk)
    return review


def _reviews_for(user):
    """Analyses the user may see: their organisation's, on documents they can reach (walls apply), or on
    contracts they can see."""
    from core.access import accessible_files
    tid = tenant_id(user)
    files = accessible_files(user).values("pk")
    contracts = contract_access.contracts_for(user).values("pk")
    return PlaybookReview.objects.filter(client_id=tid).filter(Q(file__in=files) | Q(contract__in=contracts)) \
        .select_related("playbook", "file", "contract", "matter").prefetch_related("benchmarks")


class AnalysesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        q = request.query_params
        rows = _reviews_for(request.user)
        if q.get("file_id"):
            f = _file(request.user, q["file_id"])
            rows = rows.filter(file=f) if f else rows.none()
        elif q.get("contract_id"):
            c = _contract(request.user, q["contract_id"])
            rows = rows.filter(contract=c) if c else rows.none()
        elif q.get("matter_id"):
            m = _matter(request.user, q["matter_id"])
            rows = rows.filter(matter=m) if m else rows.none()
        return Response({"results": [review_out(r, findings=False) for r in rows.distinct()[:100]]})

    def post(self, request):
        if not user_has_perm(request.user, "knowledge.analyze") and not contract_access.is_manager(request.user):
            return _err("You cannot run checks.", 403)
        contract = None
        if request.data.get("contract_id"):
            contract = _contract(request.user, request.data["contract_id"])
            if contract is None:
                return _err("Contract not found.", 404)
            file = contract.document
            if file is None:
                return _err("Attach the contract document first.")
        else:
            file = _file(request.user, request.data.get("file_id"), "view")
        if file is None:
            return _err("Document not found.", 404)
        if not can_access_file(request.user, file, "process"):
            return _err("You cannot process this document.", 403)
        pb, benchmarks, error = _sources(request)
        if error:
            return error
        review = queue_review(request.user, file, pb, benchmarks, contract=contract)
        return Response(review_out(review), status=202)


class MatterAnalysesView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from core.access import accessible_files
        if not user_has_perm(request.user, "knowledge.analyze") and not contract_access.is_manager(request.user):
            return _err("You cannot run checks.", 403)
        matter = _matter(request.user, request.data.get("matter_id"))
        if matter is None:
            return _err("Matter not found.", 404)
        pb, benchmarks, error = _sources(request)
        if error:
            return error
        files = [f for f in accessible_files(request.user).filter(project_id=matter.name, user__client_id=matter.client_id)
                 .exclude(document_type="deliverable").order_by("-id")[:MAX_MATTER_DOCUMENTS + 1]
                 if can_access_file(request.user, f, "process")]
        if not files:
            return _err("There are no documents in this matter you can check.")
        if len(files) > MAX_MATTER_DOCUMENTS:
            return _err(f"A matter check covers at most {MAX_MATTER_DOCUMENTS} documents. Check documents individually "
                        "or split the matter.")
        reviews = [queue_review(request.user, f, pb, benchmarks, matter=matter) for f in files]
        return Response({"queued": len(reviews), "results": [review_out(r, findings=False) for r in reviews]}, status=202)


class AnalysisView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        r = _reviews_for(request.user).filter(pk=pk).first() if _uuid_ok(pk) else None
        return Response(review_out(r)) if r else _err("Analysis not found.", 404)


# ── categories (buckets) and "Set as benchmark" ─────────────────────────────────────────────
def _bucket(user, pk):
    tid = tenant_id(user)
    return BenchmarkCategory.objects.filter(pk=pk, client_id=tid).first() if tid and _uuid_ok(pk) else None


def bucket_out(c):
    return {"id": str(c.id), "name": c.name, "description": c.description, "benchmark_count": c.benchmarks.count()}


def _bucket_for(user, data):
    """The category for a new benchmark: an existing one (bucket_id) or one created by name (new_bucket)."""
    if data.get("bucket_id"):
        bucket = _bucket(user, data["bucket_id"])
        if bucket is None:
            raise LookupError("That category was not found.")
        return bucket
    name = _text(data.get("new_bucket"), 120)
    if name:
        return BenchmarkCategory.objects.get_or_create(client_id=tenant_id(user), name=name,
                                                       defaults={"created_by": user})[0]
    return None


class CategoriesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not can_view_benchmarks(request.user):
            return _err("You do not have access to benchmarks.", 403)
        return Response({"results": [bucket_out(c) for c in BenchmarkCategory.objects.filter(client_id=tenant_id(request.user))],
                         "can_manage": can_manage_benchmarks(request.user)})

    def post(self, request):
        if not can_manage_benchmarks(request.user):
            return _err("Only managers and administrators can create categories.", 403)
        name = _text(request.data.get("name"), 120)
        if not name:
            return _err("Give the category a name.")
        cat, created = BenchmarkCategory.objects.get_or_create(
            client_id=tenant_id(request.user), name=name,
            defaults={"created_by": request.user, "description": str(request.data.get("description") or "")[:4000]})
        return Response(bucket_out(cat), status=201 if created else 200)


class CategoryView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        cat = _bucket(request.user, pk) if can_view_benchmarks(request.user) else None
        if cat is None:
            return _err("Category not found.", 404)
        if not can_manage_benchmarks(request.user):
            return _err("Only managers and administrators can change categories.", 403)
        if "name" in request.data:
            name = _text(request.data["name"], 120)
            if not name:
                return _err("Give the category a name.")
            if BenchmarkCategory.objects.filter(client_id=cat.client_id, name=name).exclude(pk=cat.pk).exists():
                return _err("Another category already has that name.", 409)
            cat.name = name
        if "description" in request.data:
            cat.description = str(request.data["description"] or "")[:4000]
        cat.save()
        return Response(bucket_out(cat))

    def delete(self, request, pk):
        cat = _bucket(request.user, pk) if can_view_benchmarks(request.user) else None
        if cat is None:
            return _err("Category not found.", 404)
        if not can_manage_benchmarks(request.user):
            return _err("Only managers and administrators can remove categories.", 403)
        cat.delete()          # its benchmarks stay, uncategorised (SET_NULL)
        return Response(status=204)


class SetAsBenchmarkView(APIView):
    """Freeze a document as a benchmark: a copy with its own bytes, the original's version and SHA-256 recorded,
    filed under a category and automatically under the source document's matter and service."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from django.utils import timezone
        from core.ingest import MatterBatch
        from document_versioning.utils import get_next_version_number
        if not can_manage_benchmarks(request.user):
            return _err("Only managers and administrators can set documents as benchmarks.", 403)
        source = _file(request.user, request.data.get("file_id"), "view")
        if source is None:
            return _err("Document not found.", 404)
        if not can_access_file(request.user, source, "download"):
            return _err("You need permission to download this document to set it as a benchmark.", 403)
        kind = request.data.get("kind") or "policy"
        if kind not in {k for k, _ in Benchmark.KINDS}:
            return _err("Choose a benchmark type.")
        try:
            bucket = _bucket_for(request.user, request.data)
        except LookupError as exc:
            return _err(str(exc), 404)
        batch = MatterBatch(request.user, knowledge_matter(request.user), source="benchmark")
        try:
            with transaction.atomic():
                copy, sha256 = batch.add_snapshot(source)
                b = Benchmark.objects.create(
                    client_id=tenant_id(request.user), created_by=request.user, kind=kind, file=copy, bucket=bucket,
                    name=_text(request.data.get("name"), 255) or source.filename,
                    description=str(request.data.get("description") or "")[:20000],
                    source_file=source, source_version=get_next_version_number(source),
                    source_project_id=source.project_id or "", source_service_id=source.service_id or "",
                    snapshot_at=timezone.now(), snapshot_sha256=sha256)
        except ValueError as exc:
            return _err(str(exc))
        finally:
            batch.finish()
        return Response(benchmark_out(b, full=True), status=201)


class BenchmarkTreeView(APIView):
    """Category -> matter (source project_id) -> service (source service_id) -> benchmarks."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not can_view_benchmarks(request.user):
            return _err("You do not have access to benchmarks.", 403)
        tid = tenant_id(request.user)
        rows = Benchmark.objects.filter(client_id=tid).select_related("bucket", "file")
        if request.query_params.get("active") in ("1", "true"):
            rows = rows.filter(active=True)
        tree = {}
        for b in rows:
            key = (str(b.bucket_id) if b.bucket_id else "", b.bucket.name if b.bucket_id else "Uncategorised")
            matter = b.source_project_id or "Uploaded directly"
            service = b.source_service_id or "-"
            tree.setdefault(key, {}).setdefault(matter, {}).setdefault(service, []).append(
                {"id": str(b.id), "name": b.name, "kind": b.kind, "kind_label": b.get_kind_display(), "active": b.active,
                 "term_count": b.terms.count(), "snapshot_at": b.snapshot_at, "source_version": b.source_version})
        for cat in BenchmarkCategory.objects.filter(client_id=tid):    # empty categories are shown too
            tree.setdefault((str(cat.id), cat.name), {})
        out = []
        for (cid, cname), matters in sorted(tree.items(), key=lambda kv: (kv[0][0] == "", kv[0][1].lower())):
            out.append({"id": cid or None, "name": cname, "matters": [
                {"name": m, "services": [{"name": s, "benchmarks": sorted(items, key=lambda i: i["name"].lower())}
                                         for s, items in sorted(services.items())]}
                for m, services in sorted(matters.items())]})
        return Response({"categories": out, "can_manage": can_manage_benchmarks(request.user)})
