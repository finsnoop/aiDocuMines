"""
Ethical walls API (all scoped to the caller's organisation).

GET  /api/v1/core/ethical-walls/?status=active|lifted|all       ethical_wall.view
POST /api/v1/core/ethical-walls/  {matter_id, reason, user_ids}  ethical_wall.manage
PUT  /api/v1/core/ethical-walls/<id>/people/  {user_ids}         ethical_wall.manage
POST /api/v1/core/ethical-walls/<id>/lift/  {reason}             ethical_wall.manage

Nobody can screen themselves or change a wall that screens them. Walls are lifted, never deleted.
"""
import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.access import is_platform_superuser, is_tenant_admin, tenant_id, user_has_perm
from core.models import EthicalWall, Matter

logger = logging.getLogger(__name__)
MAX_PEOPLE = 500


def can_view(user):
    return is_platform_superuser(user) or is_tenant_admin(user) or user_has_perm(user, "ethical_wall.view", "ethical_wall.manage")


def can_manage(user):
    return is_platform_superuser(user) or is_tenant_admin(user) or user_has_perm(user, "ethical_wall.manage")


def serialize(wall):
    return {"id": str(wall.id), "matter": {"id": str(wall.matter_id), "name": wall.matter.name},
            "reason": wall.reason, "active": wall.active, "excluded": list(wall.excluded.values("id", "email")),
            "created_by": getattr(wall.created_by, "email", None), "created_at": wall.created_at,
            "lifted_by": getattr(wall.lifted_by, "email", None), "lifted_at": wall.lifted_at,
            "lift_reason": wall.lift_reason}


def _people(tid, actor, raw):
    """Users of this organisation to screen; never the actor."""
    if not isinstance(raw, list) or not raw or len(raw) > MAX_PEOPLE:
        raise ValueError(f"Choose between 1 and {MAX_PEOPLE} people to screen.")
    try:
        ids = list(dict.fromkeys(int(i) for i in raw))
    except (TypeError, ValueError):
        raise ValueError("user_ids must be people's ids.")
    if actor.pk in ids:
        raise ValueError("You cannot screen yourself; ask another administrator.")
    users = list(get_user_model().objects.filter(pk__in=ids, client_id=tid))
    if len(users) != len(ids):
        raise LookupError("One or more people were not found in your organisation.")
    return users


def _error(message, code):
    return Response({"error": message}, status=code)


class EthicalWallListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not can_view(request.user):
            return _error("You cannot view ethical walls.", status.HTTP_403_FORBIDDEN)
        tid = tenant_id(request.user)
        if tid is None:
            return _error("Your account is not part of an organisation.", status.HTTP_400_BAD_REQUEST)
        walls = EthicalWall.objects.filter(client_id=tid).select_related("matter", "created_by", "lifted_by")
        state = request.query_params.get("status", "active")
        if state == "active":
            walls = walls.filter(lifted_at__isnull=True)
        elif state == "lifted":
            walls = walls.filter(lifted_at__isnull=False)
        # A screened person does not learn the details of the walls around them.
        walls = walls.exclude(excluded=request.user)
        return Response({"results": [serialize(w) for w in walls[:500]], "can_manage": can_manage(request.user)})

    def post(self, request):
        user = request.user
        if not can_manage(user):
            return _error("Only administrators and compliance officers can create ethical walls.", status.HTTP_403_FORBIDDEN)
        tid = tenant_id(user)
        if tid is None:
            return _error("Your account is not part of an organisation.", status.HTTP_400_BAD_REQUEST)
        from core.walls import walled_matter_ids
        matter = Matter.objects.filter(pk=request.data.get("matter_id"), client_id=tid).first() \
            if request.data.get("matter_id") else None
        if matter is None or matter.pk in walled_matter_ids(user):
            return _error("Matter not found.", status.HTTP_404_NOT_FOUND)
        try:
            people = _people(tid, user, request.data.get("user_ids"))
        except ValueError as exc:
            return _error(str(exc), status.HTTP_400_BAD_REQUEST)
        except LookupError as exc:
            return _error(str(exc), status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            wall = EthicalWall.objects.create(client_id=tid, matter=matter, created_by=user,
                                              reason=str(request.data.get("reason") or "")[:5000])
            wall.excluded.set(people)
        logger.info("Ethical wall %s created by user %s on matter %s (%d people)", wall.id, user.pk, matter.pk, len(people))
        from custom_authentication.views_admin import audit
        audit(request, "ethical_wall.created", "ethical_wall", wall.id, matter.name, client_id=tid,
              screened=[p.email for p in people], reason=wall.reason)
        return Response(serialize(wall), status=status.HTTP_201_CREATED)


class _WallChange(APIView):
    permission_classes = [IsAuthenticated]

    def _wall(self, request, pk):
        if not can_manage(request.user):
            return None, _error("Only administrators and compliance officers can change ethical walls.", status.HTTP_403_FORBIDDEN)
        wall = EthicalWall.objects.filter(pk=pk, client_id=tenant_id(request.user)).select_related("matter").first()
        if wall is None or wall.excluded.filter(pk=request.user.pk).exists():
            return None, _error("Ethical wall not found.", status.HTTP_404_NOT_FOUND)
        if not wall.active:
            return None, _error("This ethical wall has been lifted.", status.HTTP_400_BAD_REQUEST)
        return wall, None


class EthicalWallPeopleView(_WallChange):
    def put(self, request, pk):
        wall, error = self._wall(request, pk)
        if error:
            return error
        try:
            people = _people(wall.client_id, request.user, request.data.get("user_ids"))
        except ValueError as exc:
            return _error(str(exc), status.HTTP_400_BAD_REQUEST)
        except LookupError as exc:
            return _error(str(exc), status.HTTP_404_NOT_FOUND)
        wall.excluded.set(people)
        logger.info("Ethical wall %s people changed by user %s (%d people)", wall.id, request.user.pk, len(people))
        from custom_authentication.views_admin import audit
        audit(request, "ethical_wall.changed", "ethical_wall", wall.id, wall.matter.name, client_id=wall.client_id,
              screened=[p.email for p in people])
        return Response(serialize(wall))


class EthicalWallLiftView(_WallChange):
    def post(self, request, pk):
        wall, error = self._wall(request, pk)
        if error:
            return error
        reason = str(request.data.get("reason") or "").strip()
        if not reason:
            return _error("Say why the wall is being lifted.", status.HTTP_400_BAD_REQUEST)
        wall.lifted_at, wall.lifted_by, wall.lift_reason = timezone.now(), request.user, reason[:5000]
        wall.save(update_fields=["lifted_at", "lifted_by", "lift_reason"])
        logger.info("Ethical wall %s lifted by user %s", wall.id, request.user.pk)
        from custom_authentication.views_admin import audit
        audit(request, "ethical_wall.lifted", "ethical_wall", wall.id, wall.matter.name, client_id=wall.client_id, reason=reason[:500])
        return Response(serialize(wall))


class GovernanceOptionsView(APIView):
    """GET /api/v1/core/governance/options/?matters=<search>&people=<search>

    The organisation's matters (for holds and walls) and people (for walls), for those who may
    view holds or walls. Matters the caller is screened from are never listed.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core import views_holds
        user = request.user
        if not (can_view(user) or views_holds.can_view(user)):
            return _error("You cannot view legal holds or ethical walls.", status.HTTP_403_FORBIDDEN)
        tid = tenant_id(user)
        if tid is None:
            return _error("Your account is not part of an organisation.", status.HTTP_400_BAD_REQUEST)
        from django.db.models import Q
        from core.walls import walled_matter_ids
        term = str(request.query_params.get("matters") or "").strip()[:100]
        matters = Matter.objects.filter(client_id=tid).exclude(pk__in=walled_matter_ids(user))
        if term:
            matters = matters.filter(Q(name__icontains=term) | Q(reference__icontains=term))
        people = []
        if can_manage(user):
            who = str(request.query_params.get("people") or "").strip()[:100]
            qs = get_user_model().objects.filter(client_id=tid, is_active=True).exclude(pk=user.pk)
            if who:
                qs = qs.filter(Q(email__icontains=who) | Q(first_name__icontains=who) | Q(last_name__icontains=who))
            people = [{"id": u.pk, "email": u.email, "name": (f"{u.first_name} {u.last_name}".strip() or u.email)}
                      for u in qs.order_by("email")[:20]]
        return Response({"matters": [{"id": str(m.pk), "name": m.name, "reference": m.reference}
                                     for m in matters.order_by("name")[:50]],
                         "people": people})
