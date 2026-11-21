"""
Legal hold enforcement.

A document is on hold when an active LegalHold lists it, or covers its matter (every document
of that matter in the hold's organisation, including ones added later). While on hold it
cannot be deleted, archived, renamed, moved, edited or password-protected, by anyone.

Enforced in three layers, so no code path can bypass it:
1. The access helpers (core/access.py) refuse held documents for those actions with a clear 403.
2. Folder operations refuse when any document inside the folder is held.
3. Model guards (signals below) refuse to delete a held document, its folder links or versions,
   or to change its name, location or content, whatever code tries.
"""
from __future__ import annotations

from django.db.models import Q
from django.db.models.signals import pre_delete, pre_save
from django.dispatch import receiver
from rest_framework.exceptions import PermissionDenied

HELD_ACTIONS = frozenset({"delete", "rename", "move", "protect", "edit"})
# File fields that define a document's identity, place or content.
GUARDED_FIELDS = ("filename", "filepath", "md5_hash", "file_size", "project_id", "service_id")


class DocumentOnHold(PermissionDenied):
    default_detail = "This document is under legal hold and cannot be changed or deleted."
    default_code = "legal_hold"


def active_holds(tenant_id=None):
    from core.models import LegalHold
    qs = LegalHold.objects.filter(released_at__isnull=True)
    return qs.filter(client_id=tenant_id) if tenant_id is not None else qs


def held_q(prefix: str = "") -> Q:
    """Q over core.File (or a model reaching File via `prefix`) matching documents on hold."""
    from core.models import LegalHold
    holds = LegalHold.objects.filter(released_at__isnull=True)
    q = Q(**{f"{prefix}pk__in": LegalHold.files.through.objects.filter(
        legalhold__released_at__isnull=True).values("file_id")})
    for client_id, matter_name in holds.filter(matter__isnull=False).values_list("client_id", "matter__name"):
        q |= Q(**{f"{prefix}user__client_id": client_id, f"{prefix}project_id": matter_name})
    return q


def held_ids(file_ids) -> set:
    from core.models import File
    ids = [i for i in file_ids if i is not None]
    if not ids or not active_holds().exists():
        return set()
    return set(File.objects.filter(pk__in=ids).filter(held_q()).values_list("pk", flat=True))


def is_held(file_obj) -> bool:
    return bool(file_obj and getattr(file_obj, "pk", None) and held_ids([file_obj.pk]))


def holds_for(file_obj):
    """Active holds covering one document (names for the UI)."""
    from core.models import LegalHold
    if not file_obj:
        return LegalHold.objects.none()
    tenant = getattr(getattr(file_obj, "user", None), "client_id", None)
    return LegalHold.objects.filter(released_at__isnull=True).filter(
        Q(files=file_obj) | Q(client_id=tenant, matter__name=file_obj.project_id)).distinct()


def assert_not_held(file_obj, action: str = "edit"):
    if action in HELD_ACTIONS and is_held(file_obj):
        raise DocumentOnHold()


def assert_files_not_held(file_ids, action: str = "edit"):
    if action in HELD_ACTIONS and held_ids(file_ids):
        raise DocumentOnHold("One or more of these documents are under legal hold and cannot be changed or deleted.")


def folder_has_held_documents(folder) -> bool:
    from document_operations.models import FileFolderLink, Folder
    if not active_holds().exists():
        return False
    ids, frontier = [folder.pk], [folder.pk]
    while frontier:
        frontier = list(Folder.objects.filter(parent_id__in=frontier).values_list("pk", flat=True))
        ids.extend(frontier)
    file_ids = FileFolderLink.objects.filter(folder_id__in=ids).values_list("file_id", flat=True)
    return bool(held_ids(list(file_ids)))


def assert_folder_not_held(folder):
    if folder_has_held_documents(folder):
        raise DocumentOnHold("This folder contains documents under legal hold; it cannot be archived, moved or deleted.")


# ── Model guards: the last line, whatever code path is used ─────────────────────────────────
@receiver(pre_delete, sender="core.File", dispatch_uid="legal-hold-file-delete")
def _guard_file_delete(sender, instance, **kwargs):
    if is_held(instance):
        raise DocumentOnHold()


@receiver(pre_save, sender="core.File", dispatch_uid="legal-hold-file-change")
def _guard_file_change(sender, instance, **kwargs):
    if not instance.pk or not active_holds().exists():
        return
    before = sender.objects.filter(pk=instance.pk).values(*GUARDED_FIELDS).first()
    if before and any(before[f] != getattr(instance, f) for f in GUARDED_FIELDS) and is_held(instance):
        raise DocumentOnHold()


@receiver(pre_delete, sender="document_operations.FileFolderLink", dispatch_uid="legal-hold-link-delete")
def _guard_link_delete(sender, instance, **kwargs):
    if is_held(instance.file):
        raise DocumentOnHold()


@receiver(pre_save, sender="document_operations.FileFolderLink", dispatch_uid="legal-hold-link-change")
def _guard_link_change(sender, instance, **kwargs):
    if not instance.pk or not active_holds().exists():
        return
    before = sender.objects.filter(pk=instance.pk).values("folder_id", "is_trashed").first()
    changed = before and (before["folder_id"] != instance.folder_id or (instance.is_trashed and not before["is_trashed"]))
    if changed and is_held(instance.file):
        raise DocumentOnHold()


@receiver(pre_delete, sender="document_versioning.DocumentVersion", dispatch_uid="legal-hold-version-delete")
def _guard_version_delete(sender, instance, **kwargs):
    if is_held(getattr(instance, "file", None)):
        raise DocumentOnHold("Versions of a document under legal hold are preserved.")


# ── Audit: a deleted document leaves a tamper-evident record (its FileAuditLog rows go with it) ─────
from django.db.models.signals import post_delete  # noqa: E402


@receiver(post_delete, sender="core.File", dispatch_uid="audit-document-deleted")
def _audit_document_deleted(sender, instance, **kwargs):
    try:
        from custom_authentication.models import AdminAuditEvent
        from home_dash_analytics.agents.usage import actor
        person = actor.get()
        owner_client = sender._meta.get_field("user").related_model.objects.filter(pk=instance.user_id) \
            .values_list("client_id", flat=True).first()
        AdminAuditEvent.objects.create(
            actor=person if getattr(person, "is_authenticated", False) else None, client_id=owner_client,
            action="document.deleted", target_type="document", target_id=str(instance.pk or ""),
            target_label=str(instance.filename or "")[:255],
            details={"matter": instance.project_id, "service": instance.service_id, "owner_id": instance.user_id})
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Could not audit deletion of document %s", instance.pk)
