"""
Who may see and change contracts and obligations. Always one organisation; ethical walls on the matter apply.

See:    contract managers (contract.manage) and organisation admins see every contract of their organisation;
        others see contracts they own, requested or must approve, and contracts of matters they can see.
Change: the owner, contract managers and organisation admins.
"""
from django.db.models import Q

from core.access import (is_platform_superuser, is_tenant_admin, matters_for_user, tenant_id, user_has_perm,
                         user_role_names)


def is_manager(user) -> bool:
    return is_tenant_admin(user) or user_has_perm(user, "contract.manage")


def contracts_for(user, queryset=None):
    from core.walls import walled_matter_ids
    from .models import Contract
    qs = queryset if queryset is not None else Contract.objects.all()
    if is_platform_superuser(user) and tenant_id(user) is None:
        return qs.exclude(matter_id__in=walled_matter_ids(user))
    tid = tenant_id(user)
    if tid is None:
        return qs.none()
    qs = qs.filter(client_id=tid).exclude(matter_id__in=walled_matter_ids(user))
    if is_manager(user) or user_has_perm(user, "contract.view_all"):
        return qs
    visible_matters = matters_for_user(user).values("pk")
    return qs.filter(Q(owner=user) | Q(requested_by=user) | Q(approvals__approver=user)
                     | Q(approvals__approver_role__in=list(user_role_names(user)), approvals__status="pending")
                     | Q(matter__in=visible_matters)).distinct()


def can_view(user, contract) -> bool:
    return contracts_for(user).filter(pk=contract.pk).exists()


def can_edit(user, contract) -> bool:
    if contract.client_id != tenant_id(user) and not is_platform_superuser(user):
        return False
    return can_view(user, contract) and (is_manager(user) or contract.owner_id == user.pk)


def obligations_for(user, queryset=None):
    from .models import Obligation
    qs = queryset if queryset is not None else Obligation.objects.all()
    tid = tenant_id(user)
    if tid is None:
        return qs.none()
    qs = qs.filter(client_id=tid)
    from core.walls import walled_matter_ids
    walled = walled_matter_ids(user)
    qs = qs.exclude(matter_id__in=walled).exclude(contract__matter_id__in=walled)
    if is_manager(user) or user_has_perm(user, "contract.view_all"):
        return qs
    return qs.filter(Q(owner=user) | Q(created_by=user) | Q(contract__in=contracts_for(user).values("pk"))
                     | Q(matter__in=matters_for_user(user).values("pk"))).distinct()
