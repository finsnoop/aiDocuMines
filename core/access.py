"""
Central multi-tenancy + RBAC policy for the aiDocuMines API layer.

Every authorisation decision about *who* a caller is and *what* they may touch
should go through this module. Rules:

1. Identity, role and tenant are derived ONLY from the authenticated user
   (OAuth2 token -> request.user). Request headers such as X-Role, X-User-ID or
   X-Client-ID are never trusted for authorisation.

2. A tenant is a `custom_authentication.Client` (the customer organisation),
   reached via `user.client`. Users without a client belong to no tenant and can
   only see their own objects.

3. Platform superusers (is_superuser / SuperAdmin role) may act across tenants.
   Tenant admins (Admin role or is_staff) may act on objects owned by users in
   THEIR OWN tenant only. Everyone else may act on their own objects and on
   objects explicitly shared with them.

4. Lookups for objects the caller may not access raise 404, not 403, so object
   IDs belonging to other tenants cannot be enumerated.
"""
from __future__ import annotations

from typing import Iterable, Optional, Set

from django.db.models import Q
from django.http import Http404

# Roles that administer a tenant. Kept in sync with custom_authentication.rbac.
TENANT_ADMIN_ROLES = {"Admin"}
PLATFORM_ADMIN_ROLES = {"SuperAdmin"}
MANAGER_ROLES = {"Manager"}
DEFAULT_ROLE = "Client"


# ---------------------------------------------------------------------------
# Identity & roles
# ---------------------------------------------------------------------------

def _is_authenticated(user) -> bool:
    return bool(user is not None and getattr(user, "is_authenticated", False))


def user_role_names(user) -> Set[str]:
    """All role names held by a user (groups + roles M2M + flags). Cached per user object."""
    if not _is_authenticated(user):
        return set()
    cached = getattr(user, "_aidoc_role_names", None)
    if cached is not None:
        return cached

    names: Set[str] = set(user.groups.values_list("name", flat=True))
    roles_rel = getattr(user, "roles", None)
    if roles_rel is not None:
        try:
            names |= set(roles_rel.values_list("name", flat=True))
        except Exception:  # pragma: no cover - defensive for odd user models
            pass
    if user.is_superuser:
        names.add("SuperAdmin")
    if user.is_staff:
        names.add("Admin")
    if not names:
        names.add(DEFAULT_ROLE)

    user._aidoc_role_names = names
    return names


def primary_role(user) -> str:
    """Highest-privilege role, for display and for downstream services."""
    order = ["SuperAdmin", "Admin", "Manager", "SecurityOfficer", "ComplianceOfficer",
             "DataProtectionOfficer", "HIPAAOfficer", "SOXOfficer", "Auditor",
             "FREspecialist", "Developer", "Client", "Guest"]
    names = user_role_names(user)
    for role in order:
        if role in names:
            return role
    return next(iter(names), DEFAULT_ROLE) if names else ""


def is_platform_superuser(user) -> bool:
    return _is_authenticated(user) and (
        user.is_superuser or bool(user_role_names(user) & PLATFORM_ADMIN_ROLES)
    )


def is_tenant_admin(user) -> bool:
    """Admin of their own tenant (or platform superuser)."""
    return is_platform_superuser(user) or bool(user_role_names(user) & TENANT_ADMIN_ROLES)


def is_manager_or_admin(user) -> bool:
    return is_tenant_admin(user) or bool(user_role_names(user) & MANAGER_ROLES)


def user_permissions(user) -> Set[str]:
    """Resolved RBAC permission codenames (from role definitions + Django perms)."""
    if not _is_authenticated(user):
        return set()
    cached = getattr(user, "_aidoc_perms", None)
    if cached is not None:
        return cached

    from custom_authentication.rbac import PERMISSION_DEFINITIONS, get_role_permissions

    if is_platform_superuser(user):
        perms = {p[0] for p in PERMISSION_DEFINITIONS}
    else:
        perms = set()
        for role in user_role_names(user):
            perms |= get_role_permissions(role)
        # Explicit Django permissions granted to the user or their groups.
        for full in user.get_all_permissions():
            perms.add(full.split(".", 1)[1] if full.startswith("custom_authentication.") else full)

    user._aidoc_perms = perms
    return perms


def user_has_perm(user, *codenames: str, require_all: bool = False) -> bool:
    if not _is_authenticated(user):
        return False
    if is_platform_superuser(user):
        return True
    perms = user_permissions(user)
    checks = (c in perms for c in codenames)
    return all(checks) if require_all else any(checks)


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------

def tenant_id(user) -> Optional[int]:
    if not _is_authenticated(user):
        return None
    return getattr(user, "client_id", None)


def same_tenant(user, other_user) -> bool:
    """True when both users belong to the same (non-null) tenant."""
    if other_user is None:
        return False
    a, b = tenant_id(user), getattr(other_user, "client_id", None)
    return a is not None and a == b


def can_manage_user(actor, target) -> bool:
    """Admin actions on user accounts (disable, reset password, roles...)."""
    if not _is_authenticated(actor) or target is None:
        return False
    if is_platform_superuser(actor):
        return True
    if target.is_superuser:
        return False  # tenant admins can never touch platform superusers
    return is_tenant_admin(actor) and same_tenant(actor, target)


def tenant_users_q(actor, prefix: str = "") -> Q:
    """Q restricting a User queryset (or `<prefix>user`) to what `actor` may manage/see."""
    if is_platform_superuser(actor):
        return Q()
    tid = tenant_id(actor)
    if tid is None:
        return Q(**{f"{prefix}id": actor.id})
    return Q(**{f"{prefix}client_id": tid})


def scope_users(queryset, actor):
    return queryset.filter(tenant_users_q(actor))


# ---------------------------------------------------------------------------
# Files (core.File) — ownership, sharing and tenant-admin access
# ---------------------------------------------------------------------------

_ACTION_TO_EFFECTIVE_FLAG = {
    "view": "can_download",
    "download": "can_download",
    "process": "can_download",
    "share": "can_share",
    "rename": "can_rename",
    "move": "can_move",
    "delete": "can_delete",
}


def _shared_file_ids_q(user, action: str) -> Q:
    """Files explicitly shared with `user` via any of the platform's sharing models."""
    flag = _ACTION_TO_EFFECTIVE_FLAG.get(action, "can_download")
    q = Q(**{f"effectiveaccess__user": user, f"effectiveaccess__{flag}": True})
    if action in ("view", "download", "process"):
        q |= Q(folder_links__shared_with=user)
        q |= Q(folder_links__access_entries__user=user)
    elif action in ("rename", "move"):
        q |= Q(folder_links__access_entries__user=user,
               folder_links__access_entries__access_level__in=["write", "owner"])
    elif action in ("delete", "share"):
        q |= Q(folder_links__access_entries__user=user,
               folder_links__access_entries__access_level="owner")
    return q


def accessible_files_q(user, action: str = "view", prefix: str = "") -> Q:
    """
    Q over core.File (or a model reaching File via `prefix`, e.g. "original_file__")
    restricted to files `user` may perform `action` on.
    """
    if not _is_authenticated(user):
        return Q(pk__in=[])
    if is_platform_superuser(user):
        return Q()

    q = Q(user=user)
    if is_tenant_admin(user) and tenant_id(user) is not None:
        q |= Q(user__client_id=tenant_id(user))
    q |= _shared_file_ids_q(user, action)

    if prefix:
        return _prefix_q(q, prefix)
    return q


def _prefix_q(q: Q, prefix: str) -> Q:
    """Re-root every lookup in a Q object under `prefix`."""
    new = Q()
    new.connector = q.connector
    new.negated = q.negated
    for child in q.children:
        if isinstance(child, Q):
            new.children.append(_prefix_q(child, prefix))
        else:
            key, value = child
            new.children.append((f"{prefix}{key}", value))
    return new


def accessible_files(user, action: str = "view", queryset=None):
    from core.models import File

    qs = queryset if queryset is not None else File.objects.all()
    return qs.filter(accessible_files_q(user, action)).distinct()


def can_access_file(user, file_obj, action: str = "view") -> bool:
    if file_obj is None or not _is_authenticated(user):
        return False
    if is_platform_superuser(user) or file_obj.user_id == user.id:
        return True
    if is_tenant_admin(user) and tenant_id(user) is not None and \
            getattr(file_obj.user, "client_id", None) == tenant_id(user):
        return True
    from core.models import File
    return File.objects.filter(pk=file_obj.pk).filter(_shared_file_ids_q(user, action)).exists()


def get_file_for_user(user, file_id, action: str = "view"):
    """Fetch a core.File the user may act on, or raise Http404."""
    from core.models import File

    if file_id in (None, "", "null", "undefined"):
        raise Http404("File not found.")
    try:
        file_obj = File.objects.select_related("user").get(pk=file_id)
    except (File.DoesNotExist, ValueError, TypeError):
        raise Http404("File not found.")
    if not can_access_file(user, file_obj, action):
        raise Http404("File not found.")
    return file_obj


def get_files_for_user(user, file_ids: Iterable, action: str = "process"):
    """
    Resolve a list of file IDs for a bulk operation. Raises Http404 if ANY id is
    not accessible, so one bad id cannot silently mix another tenant's file into a batch.
    """
    from core.models import File

    ids = []
    for fid in file_ids or []:
        try:
            ids.append(int(fid))
        except (TypeError, ValueError):
            raise Http404("File not found.")
    ids = list(dict.fromkeys(ids))
    if not ids:
        return []
    files = list(accessible_files(user, action, File.objects.filter(pk__in=ids)).select_related("user"))
    if len(files) != len(ids):
        raise Http404("One or more files were not found.")
    by_id = {f.pk: f for f in files}
    return [by_id[i] for i in ids]


def get_owned_object(user, queryset, file_relation: str = "original_file", action: str = "view", **lookup):
    """
    Fetch a processing artefact (OCRFile, TranslationFile, Anonymize, runs...) whose
    access is derived from the core.File it was produced from.

        get_owned_object(request.user, TranslationFile.objects, "original_file", pk=pk)
        get_owned_object(request.user, OCRRun.objects, "ocr_files__original_file", id=run_id)
    """
    try:
        obj = queryset.filter(**lookup).filter(
            accessible_files_q(user, action, prefix=f"{file_relation}__")
        ).distinct().get()
    except Exception as exc:  # DoesNotExist, MultipleObjectsReturned, ValueError
        if exc.__class__.__name__ in ("DoesNotExist", "ValueError", "ValidationError",
                                      "MultipleObjectsReturned"):
            raise Http404("Not found.")
        raise
    return obj


def scope_by_file(user, queryset, file_relation: str = "original_file", action: str = "view"):
    """Restrict any queryset of file-derived artefacts to what `user` may see."""
    return queryset.filter(accessible_files_q(user, action, prefix=f"{file_relation}__")).distinct()


def get_scoped_or_404(user, model_or_qs, file_relation: str = "", action: str = "view", **lookup):
    """
    Generic replacement for `get_object_or_404(Model, **lookup)` on core.File or any
    artefact derived from a File. `file_relation` is the path from the model to
    core.File ("" for File itself, "original_file" for OCRFile/TranslationFile/
    Anonymize, "file" for DeAnonymize).
    """
    qs = model_or_qs.objects.all() if hasattr(model_or_qs, "objects") else model_or_qs
    prefix = f"{file_relation}__" if file_relation else ""
    try:
        obj = qs.filter(**lookup).filter(accessible_files_q(user, action, prefix=prefix)).distinct().first()
    except (ValueError, TypeError):
        obj = None
    except Exception as exc:  # invalid UUIDs etc.
        if exc.__class__.__name__ == "ValidationError":
            obj = None
        else:
            raise
    if obj is None:
        raise Http404("Not found.")
    return obj


# Processing runs (OCRRun, TranslationRun, AnonymizationRun) -------------------

RUN_FILE_RELATIONS = {
    "OCRRun": "ocr_files__original_file",
    "TranslationRun": "translation_files__original_file",
    "AnonymizationRun": "anonymized_files__original_file",
}


def runs_q(user, model) -> Q:
    """Q restricting a processing-run queryset to runs `user` may see."""
    if not _is_authenticated(user):
        return Q(pk__in=[])
    if is_platform_superuser(user):
        return Q()
    tid = tenant_id(user)
    same_or_no_tenant = (Q(client__isnull=True) | Q(client_id=tid)) if tid is not None else Q(client__isnull=True)
    owner_names = [n for n in {getattr(user, "username", None), getattr(user, "email", None)} if n]

    q = Q(client_name__in=owner_names) & same_or_no_tenant
    if is_tenant_admin(user) and tid is not None:
        q |= Q(client_id=tid)
    rel = RUN_FILE_RELATIONS.get(model.__name__ if hasattr(model, "__name__") else model.model.__name__)
    if rel:
        q |= accessible_files_q(user, "view", prefix=f"{rel}__")
    return q


def scope_runs(user, queryset):
    return queryset.filter(runs_q(user, queryset.model)).distinct()


def get_run_for_user(user, model, run_id, **extra):
    if run_id in (None, "", "null", "undefined", "N/A"):
        raise Http404("Run not found.")
    try:
        obj = model.objects.filter(pk=run_id, **extra).filter(runs_q(user, model)).distinct().first()
    except (ValueError, TypeError):
        obj = None
    except Exception as exc:
        if exc.__class__.__name__ == "ValidationError":
            obj = None
        else:
            raise
    if obj is None:
        raise Http404("Run not found.")
    return obj


def client_name_q(user, field: str = "client_name") -> Q:
    """
    For legacy models that record their owner as a username string (`client_name`):
    own rows; every row created by a user of the tenant for tenant admins; all rows
    for platform superusers.
    """
    if not _is_authenticated(user):
        return Q(pk__in=[])
    if is_platform_superuser(user):
        return Q()
    names = {n for n in (getattr(user, "username", None), getattr(user, "email", None)) if n}
    if is_tenant_admin(user) and tenant_id(user) is not None:
        from django.contrib.auth import get_user_model
        members = get_user_model().objects.filter(client_id=tenant_id(user))
        for username, email in members.values_list("username", "email"):
            names.update(n for n in (username, email) if n)
    return Q(**{f"{field}__in": sorted(names)})


def run_owner_fields(user) -> dict:
    """Fields every new processing run must carry so it is attributable to a tenant."""
    return {
        "client": getattr(user, "client", None),
        "client_name": getattr(user, "username", None) or getattr(user, "email", ""),
    }


def scope_by_owner(user, queryset, owner_field: str = "user"):
    """Restrict a queryset with a direct user FK: own rows, tenant rows for admins, all for superusers."""
    if is_platform_superuser(user):
        return queryset
    q = Q(**{owner_field: user})
    if is_tenant_admin(user) and tenant_id(user) is not None:
        q |= Q(**{f"{owner_field}__client_id": tenant_id(user)})
    return queryset.filter(q)
