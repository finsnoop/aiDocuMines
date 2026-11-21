"""
Check-out locking: while a document is checked out, only the holder may edit, rename, move or delete it.
Others get a clear 409 naming the holder. Applied in the access helpers (core/access.py) together with
legal holds, so every route follows.
"""
from rest_framework import status
from rest_framework.exceptions import APIException

LOCKED_ACTIONS = frozenset({"edit", "rename", "move", "delete", "protect"})


class DocumentCheckedOut(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "This document is checked out by someone else."
    default_code = "checked_out"


def holder(file_obj):
    """(user id, email) of whoever has the document checked out, or None."""
    from core.models import DocumentCheckout
    if not file_obj or not getattr(file_obj, "pk", None):
        return None
    row = DocumentCheckout.objects.filter(file_id=file_obj.pk).select_related("user").first()
    return (row.user_id, row.user.email) if row else None


def locked_for(user, file_obj) -> bool:
    who = holder(file_obj)
    return bool(who and who[0] != getattr(user, "pk", None))


def assert_not_locked(user, file_obj, action):
    if action in LOCKED_ACTIONS:
        who = holder(file_obj)
        if who and who[0] != getattr(user, "pk", None):
            raise DocumentCheckedOut(f"This document is checked out by {who[1]}. Ask them to check it in first.")


def assert_none_locked(user, file_ids, action):
    from core.models import DocumentCheckout
    if action in LOCKED_ACTIONS:
        other = DocumentCheckout.objects.filter(file_id__in=list(file_ids)).exclude(user_id=getattr(user, "pk", None)) \
            .select_related("user", "file").first()
        if other:
            raise DocumentCheckedOut(f"{other.file.filename} is checked out by {other.user.email}.")


def assert_folder_not_locked(user, folder):
    """Refuse folder-wide archive or delete while someone else has a document inside checked out."""
    from core.models import DocumentCheckout
    from document_operations.models import FileFolderLink, Folder
    ids, frontier = [folder.pk], [folder.pk]
    while frontier:
        frontier = list(Folder.objects.filter(parent_id__in=frontier).values_list("pk", flat=True))
        ids.extend(frontier)
    file_ids = FileFolderLink.objects.filter(folder_id__in=ids).values("file_id")
    other = DocumentCheckout.objects.filter(file_id__in=file_ids).exclude(user_id=getattr(user, "pk", None)) \
        .select_related("user", "file").first()
    if other:
        raise DocumentCheckedOut(f"{other.file.filename} in this folder is checked out by {other.user.email}.")
