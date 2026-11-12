"""
Matters: the unit of work clients think in (a deal, a case, a DSAR, an audit).

    GET    /api/v1/core/projects/                       list matters visible to the user
    POST   /api/v1/core/projects/create/                create a matter
    GET    /api/v1/core/projects/<id>/                  matter with documents, jobs and team
    PATCH  /api/v1/core/projects/<id>/                  rename / describe / close / reopen
    POST   /api/v1/core/projects/<id>/members/          {"user_ids": [...]} add team members
    DELETE /api/v1/core/projects/<id>/members/<uid>/    remove a team member
    POST   /api/v1/core/projects/<id>/upload/           multipart "file" (repeatable)

Documents belong to a matter through File.project_id == Matter.name inside the
same organisation. Older uploads that only carry a project_id are adopted into a
Matter automatically the first time the list is requested.
"""
import logging
import mimetypes
import os
from datetime import datetime

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
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

from core.access import (
    accessible_files, can_manage_matter, matters_for_user, scope_users, tenant_id,
)
from core.models import BulkJob, File, Matter, Run, Storage
from core.utils import is_safe_path_segment

logger = logging.getLogger(__name__)
User = get_user_model()

MAX_NAME = 255
PRACTICE_AREAS = [
    "Private equity and M&A", "Litigation and class actions", "Employment", "Intellectual property",
    "Regulatory, privacy and compliance", "General",
]


# ── helpers ─────────────────────────────────────────────────────────────────

def _matter_files(matter):
    """Every source document in the matter (deliverables excluded)."""
    qs = File.objects.filter(project_id=matter.name)
    if matter.client_id:
        qs = qs.filter(user__client_id=matter.client_id)
    else:
        qs = qs.filter(user_id=matter.created_by_id)
    return qs.exclude(document_type="deliverable")


def _matter_jobs(matter):
    qs = BulkJob.objects.filter(project_id=matter.name)
    if matter.client_id:
        qs = qs.filter(user__client_id=matter.client_id)
    else:
        qs = qs.filter(user_id=matter.created_by_id)
    return qs


def _person(u):
    if u is None:
        return None
    name = " ".join(filter(None, [getattr(u, "first_name", ""), getattr(u, "last_name", "")])).strip()
    return {"id": u.id, "email": u.email, "name": name or u.username or u.email}


def _summary(matter, user):
    files = _matter_files(matter)
    visible = files.filter(id__in=accessible_files(user).values("id"))
    agg = visible.aggregate(n=Count("id"), last=Max("updated_at"))
    jobs = _matter_jobs(matter)
    last_job = jobs.aggregate(last=Max("updated_at"))["last"]
    last = max(filter(None, [agg["last"], last_job, matter.updated_at]))
    return {
        "id": str(matter.id),
        "name": matter.name,
        "project_name": matter.name,          # compatibility with older screens
        "reference": matter.reference,
        "practice_area": matter.practice_area,
        "description": matter.description,
        "status": matter.status,
        "document_count": agg["n"] or 0,
        "job_count": jobs.count(),
        "running_jobs": jobs.filter(status__in=["Pending", "Queued", "Processing"]).count(),
        "member_count": matter.members.count(),
        "created_by": _person(matter.created_by),
        "created_at": matter.created_at,
        "last_activity": last,
        "can_manage": can_manage_matter(user, matter),
    }


def _adopt_legacy_matters(user):
    """Create Matter rows for project_ids that only exist on files (uploads made before matters existed)."""
    known = set(matters_for_user(user).values_list("client_id", "name"))
    rows = (
        accessible_files(user)
        .exclude(project_id="")
        .exclude(document_type="deliverable")
        .values("project_id", "user__client_id", "user_id")
        .distinct()[:500]
    )
    for row in rows:
        key = (row["user__client_id"], row["project_id"])
        if key in known or len(row["project_id"]) > MAX_NAME:
            continue
        lookup = {"client_id": row["user__client_id"], "name": row["project_id"]}
        if row["user__client_id"] is None:
            lookup["created_by_id"] = row["user_id"]
        try:
            matter, _ = Matter.objects.get_or_create(
                **lookup, defaults={"created_by_id": row["user_id"], "practice_area": "General"},
            )
        except IntegrityError:
            continue
        matter.members.add(row["user_id"])
        known.add(key)


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


# ── views ───────────────────────────────────────────────────────────────────

class MatterListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        _adopt_legacy_matters(request.user)
        qs = matters_for_user(request.user).select_related("created_by")
        state = request.query_params.get("status")
        if state in ("open", "closed"):
            qs = qs.filter(status=state)
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(reference__icontains=search))
        items = [_summary(m, request.user) for m in qs[:500]]
        items.sort(key=lambda m: m["last_activity"], reverse=True)
        return Response({"count": len(items), "results": items, "data": items,
                         "practice_areas": PRACTICE_AREAS})


class MatterCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        name, err = _clean_name(request.data.get("name") or request.data.get("project_name"))
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
        client = getattr(request.user, "client", None)
        lookup = {"client": client, "name": name}
        if client is None:
            lookup["created_by"] = request.user
        existing = Matter.objects.filter(**lookup).first()
        if existing:
            if matters_for_user(request.user).filter(id=existing.id).exists():
                return Response({"error": "A matter with this name already exists.", "matter": _summary(existing, request.user)},
                                status=status.HTTP_409_CONFLICT)
            return Response({"error": "A matter with this name already exists in your organisation."},
                            status=status.HTTP_409_CONFLICT)
        with transaction.atomic():
            matter = Matter.objects.create(
                client=client,
                name=name,
                reference=str(request.data.get("reference") or "")[:100],
                practice_area=str(request.data.get("practice_area") or "General")[:100],
                description=str(request.data.get("description") or "")[:5000],
                created_by=request.user,
            )
            matter.members.add(request.user)
            ids = request.data.get("member_ids") or []
            if isinstance(ids, list) and ids:
                matter.members.add(*scope_users(request.user, User.objects.filter(id__in=ids)))
        return Response(_summary(matter, request.user), status=status.HTTP_201_CREATED)


class MatterDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, matter_id):
        matter = _get_matter(request.user, matter_id)
        docs = (
            _matter_files(matter)
            .filter(id__in=accessible_files(request.user).values("id"))
            .select_related("user")
            .order_by("-created_at")[:2000]
        )
        jobs = _matter_jobs(matter).order_by("-created_at")[:200]
        data = _summary(matter, request.user)
        data["documents"] = [
            {
                "id": f.id, "filename": f.filename, "extension": f.extension, "file_type": f.file_type,
                "file_size": f.file_size, "status": f.status, "document_type": f.document_type,
                "uploaded_by": _person(f.user), "created_at": f.created_at,
                "has_text": bool(f.content),
            }
            for f in docs
        ]
        data["jobs"] = [
            {
                "id": str(j.id), "job_type": j.job_type, "display_name": j.get_job_type_display(),
                "service_id": j.service_id, "status": j.status, "total_files": j.total_files,
                "processed_files": j.processed_files, "failed_files": j.failed_files,
                "created_at": j.created_at, "completed_at": j.completed_at,
            }
            for j in jobs
        ]
        data["members"] = [_person(u) for u in matter.members.all().order_by("email")]
        return Response(data)

    def patch(self, request, matter_id):
        matter = _get_matter(request.user, matter_id)
        if not can_manage_matter(request.user, matter):
            return Response({"error": "Only the matter owner or an administrator can change this matter."},
                            status=status.HTTP_403_FORBIDDEN)
        if "name" in request.data and request.data.get("name") != matter.name:
            name, err = _clean_name(request.data.get("name"))
            if err:
                return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
            if Matter.objects.filter(client_id=matter.client_id, name=name).exclude(id=matter.id).exists():
                return Response({"error": "A matter with this name already exists."}, status=status.HTTP_409_CONFLICT)
            with transaction.atomic():
                _matter_files(matter).update(project_id=name)
                File.objects.filter(project_id=matter.name, document_type="deliverable",
                                    user__client_id=matter.client_id).update(project_id=name)
                _matter_jobs(matter).update(project_id=name)
                matter.name = name
        for field, limit in (("reference", 100), ("practice_area", 100), ("description", 5000)):
            if field in request.data:
                setattr(matter, field, str(request.data.get(field) or "")[:limit])
        if request.data.get("status") in ("open", "closed"):
            matter.status = request.data["status"]
        matter.save()
        return Response(_summary(matter, request.user))


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
        candidates = scope_users(request.user, User.objects.filter(id__in=ids))
        if matter.client_id:
            candidates = candidates.filter(client_id=matter.client_id)
        added = list(candidates)
        if len(added) != len(set(ids)):
            return Response({"error": "Some users were not found in your organisation."}, status=status.HTTP_400_BAD_REQUEST)
        matter.members.add(*added)
        return Response({"members": [_person(u) for u in matter.members.all().order_by("email")]})

    def delete(self, request, matter_id, user_id):
        matter = _get_matter(request.user, matter_id)
        if not can_manage_matter(request.user, matter) and request.user.id != int(user_id):
            return Response({"error": "Only the matter owner or an administrator can change the team."},
                            status=status.HTTP_403_FORBIDDEN)
        if matter.created_by_id == int(user_id):
            return Response({"error": "The matter owner cannot be removed from the team."}, status=status.HTTP_400_BAD_REQUEST)
        matter.members.remove(int(user_id))
        return Response({"members": [_person(u) for u in matter.members.all().order_by("email")]})


class MatterUploadView(APIView):
    """Upload documents into a matter. Each file is stored once per user; re-uploads are reported, not duplicated."""
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, matter_id):
        from core.utils import save_uploaded_file  # noqa: F401 (heavy module, imported lazily)
        from core.views import queue_post_upload_processing
        from document_operations.utils import register_file_folder_link

        matter = _get_matter(request.user, matter_id)
        if matter.status == "closed":
            return Response({"error": "This matter is closed. Reopen it to add documents."}, status=status.HTTP_400_BAD_REQUEST)
        uploads = request.FILES.getlist("file") or request.FILES.getlist("files")
        if not uploads:
            return Response({"error": "Choose at least one file to upload."}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        if not is_safe_path_segment(matter.name):
            return Response({"error": "Rename this matter before uploading (slashes are not allowed)."},
                            status=status.HTTP_400_BAD_REQUEST)
        base_dir = os.path.join(settings.MEDIA_ROOT, "uploads", str(tenant_id(user) or "personal"), str(user.id),
                                matter.name, "matter", stamp[:8])
        temp_dir = os.path.join(base_dir, "temp")
        os.makedirs(temp_dir, exist_ok=True)
        run = Run.objects.create(user=user, status="Pending")
        results = []

        for uploaded in uploads:
            entry = {"filename": uploaded.name}
            try:
                meta = save_uploaded_file(uploaded, temp_dir)
            except ValueError as exc:
                entry.update(status="rejected", message=str(exc))
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
        Matter.objects.filter(id=matter.id).update(updated_at=timezone.now())
        ok = sum(1 for r in results if r["status"] in ("uploaded", "added"))
        code = status.HTTP_201_CREATED if ok else status.HTTP_200_OK
        return Response({"matter_id": str(matter.id), "run_id": str(run.run_id), "uploaded": ok, "files": results}, status=code)
