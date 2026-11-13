"""
Matters: the unit of work clients think in (a deal, a case, a DSAR, an audit).

    GET    /api/v1/core/projects/                              matters (paginated)
    POST   /api/v1/core/projects/create/                       create (name, practice_area, services, ...)
    GET    /api/v1/core/projects/<id>/                         summary, services, team
    PATCH  /api/v1/core/projects/<id>/                         rename / describe / services / close / reopen
    GET    /api/v1/core/projects/<id>/documents/               documents (paginated, searchable)
    POST   /api/v1/core/projects/<id>/upload/                  multipart "file" (repeatable)
    GET    /api/v1/core/projects/<id>/jobs/                    workflow runs (paginated)
    POST   /api/v1/core/projects/<id>/run/                     run a service on the matter's documents
    GET    /api/v1/core/projects/<id>/jobs/<job>/download/     Excel / ZIP deliverable (?format=xlsx|zip)
    POST   /api/v1/core/projects/<id>/members/                 {"user_ids": [...]}
    DELETE /api/v1/core/projects/<id>/members/<uid>/

Scale: every list is paginated on the server (page/page_size, max 100); counts
for a page of matters come from a few grouped queries, never one query per
matter; documents and runs are separate paginated endpoints so a matter with
millions of files never loads at once.

Documents belong to a matter through File.project_id == Matter.name inside the
same organisation. Older uploads that only carry a project_id are adopted into
a Matter automatically (at most hourly per organisation).
"""
import logging
import mimetypes
import os
from datetime import datetime

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.http import Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import accessible_files, can_manage_matter, matters_for_user, scope_users, tenant_id
from core.models import BulkJob, BulkJobFileResult, File, Matter, Run, Storage
from core.service_catalog import ALL_SERVICES
from core.utils import is_safe_path_segment

logger = logging.getLogger(__name__)
User = get_user_model()

MAX_NAME = 255
MAX_PAGE_SIZE = 100
RUNNING = ("Pending", "Queued", "Processing")
PRACTICE_AREAS = [
    "Private equity and M&A", "Litigation and class actions", "Employment", "Intellectual property",
    "Regulatory, privacy and compliance", "General",
]
SERVICES_BY_TYPE = {s["service_type"]: s for s in ALL_SERVICES}


# ── helpers ─────────────────────────────────────────────────────────────────

def _page_params(request, default_size=25):
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        size = max(1, min(MAX_PAGE_SIZE, int(request.query_params.get("page_size", default_size))))
    except (TypeError, ValueError):
        size = default_size
    return page, size


def _paginated(items, total, page, size, **extra):
    return {"results": items, "count": total, "page": page, "page_size": size,
            "total_pages": max(1, -(-total // size)), **extra}


def _matter_scope_q(matter):
    """Q selecting rows that belong to the matter's organisation (or its creator, for personal accounts)."""
    if matter.client_id:
        return Q(user__client_id=matter.client_id)
    return Q(user_id=matter.created_by_id)


def _matter_files(matter):
    """Every source document in the matter (deliverables excluded)."""
    return File.objects.filter(_matter_scope_q(matter), project_id=matter.name).exclude(document_type="deliverable")


def _matter_jobs(matter):
    return BulkJob.objects.filter(_matter_scope_q(matter), project_id=matter.name)


def _person(u):
    if u is None:
        return None
    name = " ".join(filter(None, [getattr(u, "first_name", ""), getattr(u, "last_name", "")])).strip()
    return {"id": u.id, "email": u.email, "name": name or u.username or u.email}


def _service_info(service_type):
    s = SERVICES_BY_TYPE.get(service_type) or {}
    return {"service_type": service_type, "display_name": s.get("display_name") or service_type,
            "vertical": s.get("vertical", ""), "category": s.get("category", ""),
            "description": s.get("description", ""), "runnable": bool(s.get("handler_job_type"))}


def _clean_services(value):
    if value in (None, ""):
        return [], None
    if not isinstance(value, list):
        return None, "services must be a list of service_type values."
    unknown = [v for v in value if v not in SERVICES_BY_TYPE]
    if unknown:
        return None, f"Unknown services: {', '.join(map(str, unknown[:5]))}"
    seen, out = set(), []
    for v in value:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out[:100], None


def _summaries(matters, user):
    """Summaries for a page of matters using grouped queries (no per-matter queries)."""
    if not matters:
        return []
    names = {m.name for m in matters}
    clients = {m.client_id for m in matters if m.client_id}
    personal_owners = {m.created_by_id for m in matters if not m.client_id}
    scope = Q(user__client_id__in=clients) | Q(user_id__in=personal_owners, user__client_id__isnull=True)

    def key_of(row):
        return (row["user__client_id"] or ("u", row["user_id"]), row["project_id"])

    docs = {}
    for row in (File.objects.filter(scope, project_id__in=names).exclude(document_type="deliverable")
                .values("project_id", "user__client_id", "user_id")
                .annotate(n=Count("id"), last=Max("updated_at"))):
        n, last = docs.get(key_of(row), (0, None))
        docs[key_of(row)] = (n + row["n"], max(filter(None, [last, row["last"]]), default=None))

    jobs = {}
    for row in (BulkJob.objects.filter(scope, project_id__in=names)
                .values("project_id", "user__client_id", "user_id")
                .annotate(n=Count("id"), running=Count("id", filter=Q(status__in=RUNNING)), last=Max("updated_at"))):
        n, r, last = jobs.get(key_of(row), (0, 0, None))
        jobs[key_of(row)] = (n + row["n"], r + row["running"], max(filter(None, [last, row["last"]]), default=None))

    out = []
    for m in matters:
        key = (m.client_id or ("u", m.created_by_id), m.name)
        d_n, d_last = docs.get(key, (0, None))
        j_n, j_run, j_last = jobs.get(key, (0, 0, None))
        out.append({
            "id": str(m.id),
            "name": m.name,
            "project_name": m.name,  # compatibility with older screens
            "reference": m.reference,
            "practice_area": m.practice_area,
            "description": m.description,
            "status": m.status,
            "services": m.services or [],
            "document_count": d_n,
            "job_count": j_n,
            "running_jobs": j_run,
            "member_count": m.member_total if hasattr(m, "member_total") else m.members.count(),
            "created_by": _person(m.created_by),
            "created_at": m.created_at,
            "last_activity": max(filter(None, [d_last, j_last, m.updated_at])),
            "can_manage": can_manage_matter(user, m),
        })
    return out


def _adopt_legacy_matters(user):
    """Create Matter rows for project_ids that only exist on files. Runs at most hourly per organisation."""
    tid = tenant_id(user)
    try:
        if not cache.add(f"matters:adopted:{tid or ('u' + str(user.id))}", 1, timeout=3600):
            return
    except Exception:
        pass
    known = set(Matter.objects.filter(client_id=tid).values_list("name", flat=True)) if tid else set()
    rows = (
        accessible_files(user)
        .exclude(project_id="")
        .exclude(document_type="deliverable")
        .values("project_id", "user__client_id", "user_id")
        .distinct()[:1000]
    )
    for row in rows:
        name = row["project_id"]
        if (row["user__client_id"] and name in known) or len(name) > MAX_NAME or not is_safe_path_segment(name):
            continue
        lookup = {"client_id": row["user__client_id"], "name": name}
        if row["user__client_id"] is None:
            lookup["created_by_id"] = row["user_id"]
        try:
            matter, _ = Matter.objects.get_or_create(
                **lookup, defaults={"created_by_id": row["user_id"], "practice_area": "General"})
        except IntegrityError:
            continue
        matter.members.add(row["user_id"])
        known.add(name)


def _get_matter(user, matter_id):
    try:
        return matters_for_user(user).get(id=matter_id)
    except (Matter.DoesNotExist, ValueError, DjangoValidationError):
        raise Http404("Matter not found")


def _clean_name(value):
    name = " ".join(str(value or "").split())
    if not name:
        return None, "A matter name is required."
    if len(name) > MAX_NAME:
        return None, f"Keep the matter name under {MAX_NAME} characters."
    if not is_safe_path_segment(name):
        return None, "Matter names cannot contain slashes or '..'."
    return name, None


def _touch(matter):
    Matter.objects.filter(id=matter.id).update(updated_at=timezone.now())


def _detail(matter, user):
    data = _summaries([matter], user)[0]
    data["services"] = [_service_info(s) for s in (matter.services or [])]
    data["members"] = [_person(u) for u in matter.members.all().order_by("email")[:200]]
    return data


# ── views ───────────────────────────────────────────────────────────────────

class MatterListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _adopt_legacy_matters(request.user)
        page, size = _page_params(request)
        qs = matters_for_user(request.user).select_related("created_by")
        state = request.query_params.get("status")
        if state in ("open", "closed"):
            qs = qs.filter(status=state)
        area = (request.query_params.get("practice_area") or "").strip()
        if area:
            qs = qs.filter(practice_area=area)
        search = (request.query_params.get("search") or "").strip()[:200]
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(reference__icontains=search))
        total = qs.count()
        matters = list(qs.annotate(member_total=Count("members", distinct=True))
                       .order_by("-updated_at")[(page - 1) * size: page * size])
        items = _summaries(matters, request.user)
        return Response(_paginated(items, total, page, size, data=items, practice_areas=PRACTICE_AREAS))


class MatterCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        name, err = _clean_name(request.data.get("name") or request.data.get("project_name"))
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
        services, err = _clean_services(request.data.get("services"))
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
        client = getattr(request.user, "client", None)
        lookup = {"client": client, "name": name}
        if client is None:
            lookup["created_by"] = request.user
        existing = Matter.objects.filter(**lookup).first()
        if existing:
            body = {"error": "A matter with this name already exists."}
            if matters_for_user(request.user).filter(id=existing.id).exists():
                body["matter"] = _summaries([existing], request.user)[0]
            return Response(body, status=status.HTTP_409_CONFLICT)
        with transaction.atomic():
            matter = Matter.objects.create(
                client=client,
                name=name,
                reference=str(request.data.get("reference") or "")[:100],
                practice_area=str(request.data.get("practice_area") or "General")[:100],
                description=str(request.data.get("description") or "")[:5000],
                services=services,
                created_by=request.user,
            )
            matter.members.add(request.user)
            ids = request.data.get("member_ids") or []
            if isinstance(ids, list) and ids:
                matter.members.add(*scope_users(User.objects.filter(id__in=ids), request.user))
        return Response(_detail(matter, request.user), status=status.HTTP_201_CREATED)


class MatterDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, matter_id):
        return Response(_detail(_get_matter(request.user, matter_id), request.user))

    def patch(self, request, matter_id):
        matter = _get_matter(request.user, matter_id)
        if not can_manage_matter(request.user, matter):
            return Response({"error": "Only the matter owner or an administrator can change this matter."},
                            status=status.HTTP_403_FORBIDDEN)
        if "services" in request.data:
            services, err = _clean_services(request.data.get("services"))
            if err:
                return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
            matter.services = services
        if "name" in request.data and request.data.get("name") != matter.name:
            name, err = _clean_name(request.data.get("name"))
            if err:
                return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
            if Matter.objects.filter(client_id=matter.client_id, name=name).exclude(id=matter.id).exists():
                return Response({"error": "A matter with this name already exists."}, status=status.HTTP_409_CONFLICT)
            with transaction.atomic():
                File.objects.filter(_matter_scope_q(matter), project_id=matter.name).update(project_id=name)
                _matter_jobs(matter).update(project_id=name)
                matter.name = name
        for field, limit in (("reference", 100), ("practice_area", 100), ("description", 5000)):
            if field in request.data:
                setattr(matter, field, str(request.data.get(field) or "")[:limit])
        if request.data.get("status") in ("open", "closed"):
            matter.status = request.data["status"]
        matter.save()
        return Response(_detail(matter, request.user))


class MatterDocumentsView(APIView):
    """Documents in a matter the caller can read, newest first, paginated and searchable."""
    permission_classes = [IsAuthenticated]
    ORDERING = {"name": "filename", "-name": "-filename", "size": "file_size", "-size": "-file_size",
                "oldest": "created_at", "newest": "-created_at"}

    def get(self, request, matter_id):
        matter = _get_matter(request.user, matter_id)
        page, size = _page_params(request, default_size=50)
        qs = _matter_files(matter).filter(id__in=accessible_files(request.user).values("id"))
        search = (request.query_params.get("search") or "").strip()[:200]
        if search:
            qs = qs.filter(filename__icontains=search)
        ordering = self.ORDERING.get(request.query_params.get("ordering", ""), "-created_at")
        total = qs.count()
        rows = qs.select_related("user").order_by(ordering, "-id")[(page - 1) * size: page * size]
        items = [{
            "id": f.id, "filename": f.filename, "extension": f.extension, "file_type": f.file_type,
            "file_size": f.file_size, "status": f.status, "document_type": f.document_type,
            "uploaded_by": _person(f.user), "created_at": f.created_at,
        } for f in rows]
        return Response(_paginated(items, total, page, size))


class MatterJobsView(APIView):
    """Workflow runs for a matter (whole team), newest first, paginated."""
    permission_classes = [IsAuthenticated]

    def get(self, request, matter_id):
        matter = _get_matter(request.user, matter_id)
        page, size = _page_params(request, default_size=25)
        qs = _matter_jobs(matter).select_related("user").order_by("-created_at")
        total = qs.count()
        items = [{
            "id": str(j.id), "job_type": j.job_type, "service_id": j.service_id,
            "display_name": (SERVICES_BY_TYPE.get(j.service_id) or {}).get("display_name") or j.get_job_type_display(),
            "status": j.status, "total_files": j.total_files, "processed_files": j.processed_files,
            "failed_files": j.failed_files, "created_at": j.created_at, "completed_at": j.completed_at,
            "run_by": _person(j.user),
        } for j in qs[(page - 1) * size: page * size]]
        return Response(_paginated(items, total, page, size))


class MatterRunView(APIView):
    """
    Run a catalogue service on the matter's documents (all of them, or `file_ids`).
    Any team member can run on any document in the matter, not just their own uploads.
    """
    permission_classes = [IsAuthenticated]
    MAX_FILES = 50000

    def post(self, request, matter_id):
        from core.bulk_registry import get_bulk_handler
        from core.tasks_bulk import execute_bulk_job

        matter = _get_matter(request.user, matter_id)
        if matter.status == "closed":
            return Response({"error": "This matter is closed. Reopen it to run services."}, status=400)
        service_type = request.data.get("service_type")
        svc = SERVICES_BY_TYPE.get(service_type)
        if not svc:
            return Response({"error": "Choose a service from the catalogue."}, status=400)
        job_type = svc.get("handler_job_type")
        if not job_type or not get_bulk_handler(job_type):
            return Response({"error": f"'{svc.get('display_name')}' cannot be run on a set of documents yet."}, status=400)

        qs = _matter_files(matter).filter(id__in=accessible_files(request.user, "process").values("id"))
        ids = request.data.get("file_ids")
        if ids:
            if not isinstance(ids, list):
                return Response({"error": "file_ids must be a list."}, status=400)
            qs = qs.filter(id__in=[int(i) for i in ids if str(i).isdigit()])
        accepted = svc.get("accepted_extensions")
        if accepted:
            qs = qs.filter(extension__in=[e.lower() for e in accepted])
        file_ids = list(qs.values_list("id", flat=True)[: self.MAX_FILES + 1])
        if not file_ids:
            return Response({"error": "No documents in this matter can be processed by this service."}, status=400)
        if len(file_ids) > self.MAX_FILES:
            return Response({"error": f"Select at most {self.MAX_FILES:,} documents per run."}, status=400)

        with transaction.atomic():
            job = BulkJob.objects.create(
                user=request.user, job_type=job_type, status="Pending", total_files=len(file_ids),
                project_id=matter.name, service_id=service_type,
                input_parameters=request.data.get("parameters") or {},
            )
            BulkJobFileResult.objects.bulk_create(
                (BulkJobFileResult(bulk_job=job, file_id=fid, status="Pending") for fid in file_ids), batch_size=1000)
            transaction.on_commit(lambda: execute_bulk_job.delay(str(job.id)))
        _touch(matter)
        return Response({"job_id": str(job.id), "status": "Queued", "total_files": len(file_ids),
                         "display_name": svc.get("display_name")}, status=201)


class MatterJobDownloadView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, matter_id, job_id):
        from core.views_bulk import deliverable_response

        matter = _get_matter(request.user, matter_id)
        try:
            job = _matter_jobs(matter).get(id=job_id)
        except (BulkJob.DoesNotExist, ValueError, DjangoValidationError):
            raise Http404("Run not found")
        return deliverable_response(job, request.query_params.get("format"))


class MatterMembersView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, matter_id):
        matter = _get_matter(request.user, matter_id)
        if not can_manage_matter(request.user, matter):
            return Response({"error": "Only the matter owner or an administrator can change the team."},
                            status=status.HTTP_403_FORBIDDEN)
        ids = request.data.get("user_ids") or []
        if not isinstance(ids, list) or not ids:
            return Response({"error": "user_ids must be a non-empty list."}, status=status.HTTP_400_BAD_REQUEST)
        candidates = scope_users(User.objects.filter(id__in=ids), request.user)
        if matter.client_id:
            candidates = candidates.filter(client_id=matter.client_id)
        added = list(candidates)
        if len(added) != len(set(ids)):
            return Response({"error": "Some users were not found in your organisation."}, status=status.HTTP_400_BAD_REQUEST)
        matter.members.add(*added)
        return Response({"members": [_person(u) for u in matter.members.all().order_by("email")[:200]]})

    def delete(self, request, matter_id, user_id):
        matter = _get_matter(request.user, matter_id)
        if not can_manage_matter(request.user, matter) and request.user.id != int(user_id):
            return Response({"error": "Only the matter owner or an administrator can change the team."},
                            status=status.HTTP_403_FORBIDDEN)
        if matter.created_by_id == int(user_id):
            return Response({"error": "The matter owner cannot be removed from the team."}, status=status.HTTP_400_BAD_REQUEST)
        matter.members.remove(int(user_id))
        return Response({"members": [_person(u) for u in matter.members.all().order_by("email")[:200]]})


class MatterUploadView(APIView):
    """Upload documents into a matter. Each file is stored once per user; re-uploads are reported, not duplicated."""
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, matter_id):
        from core.utils import save_uploaded_file
        from core.views import queue_post_upload_processing
        from document_operations.utils import register_file_folder_link

        matter = _get_matter(request.user, matter_id)
        if matter.status == "closed":
            return Response({"error": "This matter is closed. Reopen it to add documents."}, status=status.HTTP_400_BAD_REQUEST)
        uploads = request.FILES.getlist("file") or request.FILES.getlist("files")
        if not uploads:
            return Response({"error": "Choose at least one file to upload."}, status=status.HTTP_400_BAD_REQUEST)
        if not is_safe_path_segment(matter.name):
            return Response({"error": "Rename this matter before uploading (slashes are not allowed)."},
                            status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        base_dir = os.path.join(settings.MEDIA_ROOT, "uploads", str(tenant_id(user) or "personal"), str(user.id),
                                matter.name, "matter", stamp[:8])
        temp_dir = os.path.join(base_dir, "temp")
        os.makedirs(temp_dir, exist_ok=True)
        run = Run.objects.create(user=user, status="Pending")
        results = []

        from custom_authentication.policies import upload_violation

        for uploaded in uploads:
            entry = {"filename": uploaded.name}
            reason = upload_violation(user, uploaded.name, uploaded.size)
            if reason:
                entry.update(status="rejected", message=reason)
                results.append(entry)
                continue
            try:
                meta = save_uploaded_file(uploaded, temp_dir)
            except ValueError as exc:
                entry.update(status="rejected", message=str(exc))
                results.append(entry)
                continue
            if not meta.get("file_size"):
                os.remove(meta["file_path"])
                entry.update(status="rejected", message="This file is empty.")
                results.append(entry)
                continue

            existing = File.objects.filter(user=user, md5_hash=meta["md5_hash"]).first()
            if existing:
                os.remove(meta["file_path"])
                if existing.project_id == matter.name:
                    entry.update(status="duplicate", file_id=existing.id, message="Already in this matter.")
                    results.append(entry)
                    continue
                copy = File.objects.create(
                    run=run, storage=existing.storage, filename=existing.filename, filepath=existing.filepath,
                    file_size=existing.file_size, file_type=existing.file_type, md5_hash=None,
                    user=user, project_id=matter.name, service_id="matter", origin_file=existing,
                    extension=existing.extension, content=existing.content, status=existing.status,
                )
                register_file_folder_link(copy)
                entry.update(status="added", file_id=copy.id, message="Added from your existing documents.")
                results.append(entry)
                continue

            final_path = os.path.join(base_dir, f"{stamp[8:]}_{meta['filename']}")
            os.rename(meta["file_path"], final_path)
            storage = Storage.objects.create(user=user, content_type=ContentType.objects.get_for_model(run),
                                             object_id=run.run_id, upload_storage_location=final_path)
            ext = os.path.splitext(meta["filename"])[1].lstrip(".").lower()[:10] or "unknown"
            try:
                fresh = File.objects.create(
                    run=run, storage=storage, filename=meta["filename"], filepath=final_path,
                    file_size=meta["file_size"],
                    file_type=meta.get("file_type") or mimetypes.guess_type(meta["filename"])[0] or "application/octet-stream",
                    md5_hash=meta["md5_hash"], user=user, project_id=matter.name, service_id="matter", extension=ext,
                )
            except IntegrityError:
                entry.update(status="duplicate", message="Already uploaded.")
                results.append(entry)
                continue
            register_file_folder_link(fresh)
            try:
                queue_post_upload_processing(user, run, fresh, matter.name, "matter")
            except Exception:  # processing is best-effort; the document is stored either way
                logger.exception("Post-upload processing could not be queued for file %s", fresh.id)
            entry.update(status="uploaded", file_id=fresh.id, message="Uploaded.")
            results.append(entry)

        try:
            os.rmdir(temp_dir)
        except OSError:
            pass
        _touch(matter)
        ok = sum(1 for r in results if r["status"] in ("uploaded", "added"))
        code = status.HTTP_201_CREATED if ok else status.HTTP_200_OK
        return Response({"matter_id": str(matter.id), "run_id": str(run.run_id), "uploaded": ok, "files": results}, status=code)


class MatterPeopleView(APIView):
    """GET /projects/<id>/people/?search=  colleagues in the matter's organisation (max 20) to add to the team."""
    permission_classes = [IsAuthenticated]

    def get(self, request, matter_id):
        matter = _get_matter(request.user, matter_id)
        if not can_manage_matter(request.user, matter):
            return Response({"results": []})
        term = (request.query_params.get("search") or "").strip()[:100]
        qs = scope_users(User.objects.filter(is_active=True), request.user)
        if matter.client_id:
            qs = qs.filter(client_id=matter.client_id)
        if term:
            qs = qs.filter(Q(email__icontains=term) | Q(first_name__icontains=term) | Q(last_name__icontains=term))
        qs = qs.exclude(id__in=matter.members.values("id")).order_by("email")[:20]
        return Response({"results": [_person(u) for u in qs]})
