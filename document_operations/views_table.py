"""
GET /api/v1/documents/table/  one flat, sortable, filterable list of every document the user can open.

Query: page, page_size (max 100), search, project_id, service_id, extension,
       ordering (name|-name|size|-size|modified|-modified|matter|-matter), scope (all|mine|shared)

Built for very large libraries: rows come one page at a time from an indexed
ordering, and the total is counted exactly only up to 10,000 (then "10,000+"),
because exact counts over billions of rows are slow and nobody pages that far.
"""
from django.db.models import Case, IntegerField, Q, Value, When
from django.http import Http404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import can_access_file, can_access_folder, get_folder_for_user, is_platform_superuser, is_tenant_admin, tenant_id, user_role_names
from .document_query import COUNT_CAP, FILTER_FIELDS, accessible_document_query, document_filters, folder_ids_below

MAX_PAGE_SIZE = 100


def _int(v, default, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


class DocumentTableView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        qp = request.query_params
        page = _int(qp.get("page"), 1, 1, 100_000)
        size = _int(qp.get("page_size"), 50, 1, MAX_PAGE_SIZE)

        qs = accessible_document_query(user, {name: qp[name] for name in FILTER_FIELDS if name in qp})
        start = (page - 1) * size
        rows = list(qs.select_related("user")[start: start + size + 1])
        has_next = len(rows) > size
        rows = rows[:size]

        from core.holds import held_ids
        held = held_ids([f.id for f in rows])
        capped = qs.order_by()[:COUNT_CAP + 1].count() if page == 1 or qp.get("with_count") else None
        total = None if capped is None else min(capped, COUNT_CAP)

        return Response({
            "results": [{
                "id": f.id,
                "filename": f.filename,
                "extension": (f.extension or "").lower(),
                "file_type": f.file_type,
                "file_size": f.file_size,
                "matter": f.project_id,
                "service": f.service_id,
                "document_type": f.document_type,
                "status": f.status,
                "owner": {"id": f.user_id, "name": (f.user.username or f.user.email) if f.user else ""},
                "is_mine": f.user_id == user.id,
                "on_hold": f.id in held,
                "created_at": f.created_at,
                "updated_at": f.updated_at,
            } for f in rows],
            "page": page,
            "page_size": size,
            "has_next": has_next,
            "count": total,
            "count_is_capped": capped is not None and capped > COUNT_CAP,
        })


class DocumentFacetsView(APIView):
    """
    GET /api/v1/documents/table/facets/  matters, services and file types for the table's filters (bounded).
    ?project_id=X narrows services and file types to that project (used by the Assistant's topic picker).
    """
    permission_classes = [IsAuthenticated]
    LIMIT = 200

    def get(self, request):
        filters = {"project_id": request.query_params["project_id"]} if "project_id" in request.query_params else {}
        qs = accessible_document_query(request.user, filters).order_by()

        def distinct(field):
            return sorted({v for v in qs.values_list(field, flat=True).distinct()[: self.LIMIT] if v})

        return Response({
            "matters": distinct("project_id"),
            "services": distinct("service_id"),
            "extensions": [e.lower() for e in distinct("extension")],
        })


class DocumentActionContextView(APIView):
    """Resolve one row lazily to the link identity used by Sidebar operations.

    A file can have aliases in private folders. Reading the file is insufficient
    to expose one of those folders, so only links in viewable folders are used.
    No links are fetched per row during normal table pagination.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, file_id):
        from core.utils import get_logical_path
        from document_operations.models import EffectiveAccess, FileFolderLink

        user = request.user
        f = accessible_document_query(user).select_related("user").filter(pk=file_id).first()
        if f is None:
            raise Http404("File not found.")
        filters = document_filters({"folder": request.query_params.get("folder")})
        links = FileFolderLink.objects.filter(file=f, is_trashed=False, folder__is_trashed=False)
        if not is_platform_superuser(user):
            folders = Q(folder__user=user)
            tid = tenant_id(user)
            if tid is not None:
                same_tenant = Q(folder__user__client_id=tid)
                if is_tenant_admin(user):
                    folders |= same_tenant
                else:
                    granted = EffectiveAccess.objects.filter(user=user, can_download=True, folder__isnull=False)
                    folders |= same_tenant & Q(folder_id__in=granted.values("folder_id"))
            links = links.filter(folders)
        if filters.get("folder"):
            import uuid
            folder = get_folder_for_user(user, uuid.UUID(filters["folder"]), "view")
            if folder.is_trashed:
                raise Http404("Folder not found.")
            links = links.filter(folder_id__in=folder_ids_below(folder.pk))
        preference = []
        if filters.get("folder"):
            preference.append(When(folder_id=filters["folder"], then=Value(0)))
        preference.append(When(folder__user=user, then=Value(1)))
        link = links.select_related("folder").order_by(
            Case(*preference, default=Value(2), output_field=IntegerField()), "pk").first()
        restricted = user_role_names(user) == {"Guest"}
        permissions = {"read": True, "download": can_access_file(user, f, "download"),
                       "versions": link is not None}
        permissions.update({action: not restricted and can_access_file(user, f, action)
                            and (link is not None or action == "process")
                            for action in ("rename", "move", "delete", "share", "process", "duplicate", "zip", "protect", "restore")})
        permissions["copy"] = not restricted and permissions["download"] and link is not None
        from core.holds import HELD_ACTIONS, is_held
        on_hold = is_held(f)
        if on_hold:  # legal hold: nothing that changes or removes the document
            for action in HELD_ACTIONS & set(permissions):
                permissions[action] = False
        permissions["legal_hold"] = on_hold
        from core.checkout import LOCKED_ACTIONS, holder
        who = holder(f)
        if who and who[0] != user.pk:
            for action in LOCKED_ACTIONS & set(permissions):
                permissions[action] = False
        permissions["checked_out_by"] = who[1] if who else None
        permissions["checkout"] = not on_hold and not who and not restricted and can_access_file(user, f, "edit")
        permissions["checkin"] = bool(who) and (who[0] == user.pk or is_tenant_admin(user) or is_platform_superuser(user))
        permissions["duplicate"] = permissions["duplicate"] and can_access_folder(user, link.folder, "manage")
        if permissions["move"] and f.user_id != user.pk and not is_tenant_admin(user):
            permissions["move"] = can_access_file(user, f, "share")
        node = None
        if link is not None:
            url = get_logical_path(f.filepath, f.filename)
            node = {
                "key": str(link.pk), "id": link.pk, "fileFolderLinkId": link.pk,
                "label": f.filename, "name": f.filename,
                "extention": (f.extension or "").lower(), "size": f.file_size,
                "download_url": url, "created_at": f.created_at, "updated_at": f.updated_at,
                "data": {"type": "file", "file_id": f.id, "project_id": f.project_id,
                         "service_id": f.service_id, "folder_id": str(link.folder_id),
                         "parent_folder_id": str(link.folder_id), "file_size": f.file_size,
                         "download_url": url, "is_trashed": False},
            }
        return Response({"node": node, "permissions": permissions,
                         "file": {"id": f.id, "filename": f.filename, "extension": f.extension,
                                  "file_size": f.file_size, "matter": f.project_id, "service": f.service_id,
                                  "created_at": f.created_at, "updated_at": f.updated_at}})
