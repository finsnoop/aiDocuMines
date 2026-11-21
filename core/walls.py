"""
Ethical walls (information barriers).

A person excluded by an active wall cannot see or act on the walled matter, its folders or any of
its documents (every document of that matter in the wall's organisation, including later ones),
whatever their role, matter membership or explicit shares. Applied in the central access layer
(core/access.py), so listings, search, the assistant, previews, downloads and every action follow.

The walled matters of a user are computed once per user object (like resolved permissions).
"""
from __future__ import annotations

from django.db.models import Q


def walled_matters(user) -> tuple:
    """((client_id, matter_name, matter_id), ...) the user is screened from."""
    if user is None or not getattr(user, "is_authenticated", False) or not getattr(user, "pk", None):
        return ()
    cached = getattr(user, "_aidoc_walls", None)
    if cached is not None:
        return cached
    from core.models import EthicalWall
    walls = tuple(EthicalWall.objects.filter(excluded=user, lifted_at__isnull=True)
                  .values_list("client_id", "matter__name", "matter_id"))
    try:
        user._aidoc_walls = walls
    except Exception:
        pass
    return walls


def forget(user):
    """Drop the cached walls (after a wall changes for this user object)."""
    if hasattr(user, "_aidoc_walls"):
        try:
            delattr(user, "_aidoc_walls")
        except Exception:
            pass


def walled_files_q(user, prefix: str = ""):
    """Q over core.File matching documents the user is screened from, or None when there are none."""
    walls = walled_matters(user)
    if not walls:
        return None
    q = Q(pk__in=[])
    for client_id, name, _ in walls:
        q |= Q(**{f"{prefix}user__client_id": client_id, f"{prefix}project_id": name})
    return q


def is_walled_file(user, file_obj) -> bool:
    walls = walled_matters(user)
    if not walls or file_obj is None:
        return False
    owner_client = getattr(getattr(file_obj, "user", None), "client_id", None)
    return any(owner_client == client_id and file_obj.project_id == name for client_id, name, _ in walls)


def is_walled_folder(user, folder) -> bool:
    walls = walled_matters(user)
    if not walls or folder is None:
        return False
    owner_client = getattr(getattr(folder, "user", None), "client_id", None)
    return any(owner_client == client_id and folder.project_id == name for client_id, name, _ in walls)


def walled_matter_ids(user) -> list:
    return [matter_id for _, _, matter_id in walled_matters(user)]


def exclude_walled_folders(queryset, user, prefix: str = ""):
    """Drop folders of walled matters from a Folder queryset (or a model reaching Folder via `prefix`)."""
    walls = walled_matters(user)
    if not walls:
        return queryset
    q = Q(pk__in=[])
    for client_id, name, _ in walls:
        q |= Q(**{f"{prefix}user__client_id": client_id, f"{prefix}project_id": name})
    return queryset.exclude(q)
