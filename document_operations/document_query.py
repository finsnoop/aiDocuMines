"""One tenant/ACL-aware document scope for the table and its Assistant.

Filters describe documents, never the identity or tenant of the caller.  Keep
pagination outside this module so asking about a table includes every match.
"""
import uuid
from collections.abc import Mapping

from django.db.models import Exists, OuterRef, Q
from rest_framework.exceptions import ValidationError

from core.access import accessible_files, get_folder_for_user

COUNT_CAP = 10_000
MAX_FOLDERS = 5_000
ORDERING = {
    "name": "filename", "-name": "-filename",
    "size": "file_size", "-size": "-file_size",
    "modified": "updated_at", "-modified": "-updated_at",
    "matter": "project_id", "-matter": "-project_id",
    "created": "created_at", "-created": "-created_at",
}
FILTER_FIELDS = {"scope", "search", "project_id", "service_id", "extension", "folder", "ordering"}


def document_filters(raw=None):
    """Validate the same small filter schema for JSON scopes and query strings."""
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValidationError({"filters": "Provide an object of document filters."})
    unknown = set(raw) - FILTER_FIELDS
    if unknown:
        raise ValidationError({"filters": "Unknown document filter."})
    result = {}
    for name, limit in (("scope", 10), ("search", 200), ("project_id", 255),
                        ("service_id", 255), ("extension", 10), ("folder", 36), ("ordering", 20)):
        value = raw.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
            raise ValidationError({name: f"Provide text of at most {limit} characters."})
        value = value.strip()
        if value:
            result[name] = value
    if result.get("scope", "all") not in {"all", "mine", "shared"}:
        raise ValidationError({"scope": "Choose all, mine or shared."})
    if "ordering" in result and result["ordering"] not in ORDERING:
        raise ValidationError({"ordering": "Unknown document ordering."})
    if "folder" in result:
        try:
            result["folder"] = str(uuid.UUID(result["folder"]))
        except (ValueError, TypeError, AttributeError):
            raise ValidationError({"folder": "Unknown folder."})
    return result


def folder_ids_below(root_id):
    """Bounded breadth-first folder traversal, excluding archived descendants."""
    from document_operations.models import Folder

    ids, seen, frontier = [root_id], {root_id}, [root_id]
    while frontier and len(ids) < MAX_FOLDERS:
        remaining = MAX_FOLDERS - len(ids)
        children = Folder.objects.filter(parent_id__in=frontier, is_trashed=False).exclude(pk__in=seen).order_by("id")
        frontier = list(children.values_list("id", flat=True)[:remaining + 1])
        if len(frontier) > remaining:
            raise ValidationError({"folder": "This location contains too many folders. Choose a more specific location."})
        ids.extend(frontier)
        seen.update(frontier)
    if frontier and len(ids) >= MAX_FOLDERS and Folder.objects.filter(
            parent_id__in=frontier, is_trashed=False).exclude(pk__in=seen).exists():
        raise ValidationError({"folder": "This location contains too many folders. Choose a more specific location."})
    return ids


def accessible_document_query(user, filters=None):
    """Accessible source documents matching validated filters, in table order.

    Legacy unlinked documents remain visible. Documents with folder links only
    in the archive are omitted, just like archived links in the Locations tree.
    EXISTS avoids duplicating documents with multiple folder aliases.
    """
    from document_operations.models import FileFolderLink

    filters = document_filters(filters)
    links = FileFolderLink.objects.filter(file_id=OuterRef("pk"))
    active = links.filter(is_trashed=False, folder__is_trashed=False)
    qs = accessible_files(user).exclude(document_type="deliverable").filter(~Exists(links) | Exists(active))
    scope = filters.get("scope", "all")
    if scope == "mine":
        qs = qs.filter(user=user)
    elif scope == "shared":
        qs = qs.exclude(user=user)
    if filters.get("search"):
        qs = qs.filter(Q(filename__icontains=filters["search"]) | Q(project_id__icontains=filters["search"]))
    for name in ("project_id", "service_id"):
        if filters.get(name):
            qs = qs.filter(**{name: filters[name]})
    if filters.get("extension"):
        qs = qs.filter(extension__iexact=filters["extension"])
    if filters.get("folder"):
        folder = get_folder_for_user(user, uuid.UUID(filters["folder"]), "view")
        if folder.is_trashed:
            from django.http import Http404
            raise Http404("Folder not found.")
        in_folder = FileFolderLink.objects.filter(
            folder_id__in=folder_ids_below(folder.pk),
            is_trashed=False, folder__is_trashed=False)
        qs = qs.filter(id__in=in_folder.values("file_id"))
    return qs.order_by(ORDERING.get(filters.get("ordering"), "-updated_at"), "-id")


def document_filter_label(filters):
    """An authoritative, readable description without trusting client row data."""
    parts = [{"all": "All accessible documents", "mine": "My documents", "shared": "Shared documents"}[
        filters.get("scope", "all")]]
    for name, label in (("search", "Search"), ("project_id", "Matter"), ("service_id", "Service"),
                        ("extension", "Type")):
        if filters.get(name):
            parts.append(f"{label}: {filters[name]}")
    if filters.get("folder"):
        # A file share does not grant permission to disclose its private folder's name.
        parts.append("Selected location and subfolders")
    return " · ".join(parts)[:1000]
